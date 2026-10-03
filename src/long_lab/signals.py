"""曝光与去标识化信号管线。

核心规则：
- 五类信号保留各自独立时间窗（见 contracts.WINDOW_DEFINITIONS），窗口 id 由
  (曝光, 信号种类) 确定性确定，收藏后打开以收藏时刻为锚，跨日看完要求次日且 7 日内；
- 信号 provenance 默认为 organic；互刷/运营投放/粉丝集中回访必须显式标记或经
  SIGNAL_RECLASSIFIED 再分类，投影时绝不计入自然价值；
- 到达时间晚于窗口关闭时间的迟到数据：仅当窗口未封账且实验未结束时，
  以 WINDOW_PARTIALLY_CORRECTED 入账并给出指标 delta；封账后拒绝修正；
- 处置中（downrank/remove）内容在 treatment 臂的自然曝光被硬门栏拦截。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .catalog import ContentCatalog
from .contracts import WINDOW_DEFINITIONS
from .events import Event, EventStore
from .timekeeping import parse_dt

GRACE_HOURS = 24  # 窗口关闭后接受迟到纠正的宽限


class SignalError(RuntimeError):
    pass


class WindowExpired(SignalError):
    pass


class LedgerFinalized(SignalError):
    pass


class RiskGateBlocked(SignalError):
    pass


class LateDataRejected(SignalError):
    pass


def window_id_for(exposure_id: str, kind: str) -> str:
    return f"{exposure_id}:{kind}"


@dataclass
class WindowState:
    window_id: str
    exposure_id: str
    content_version_id: str
    experiment_id: str
    arm: str
    kind: str
    closes_at: str
    finalized: bool = False
    signal: dict | None = None
    provenance: str | None = None
    corrections: list[dict] = None  # type: ignore[assignment]
    close_event: Event | None = None

    def __post_init__(self) -> None:
        self.corrections = []


class SignalPipeline:
    def __init__(self, store: EventStore, catalog: ContentCatalog) -> None:
        self.store = store
        self.catalog = catalog

    # ---- 曝光 ----
    def record_exposure(
        self, exposure_id: str, content_version_id: str, user_pseudo: str,
        experiment_id: str, arm: str, occurred_at: str,
        usage_time_segment: str, *, delivery_channel: str = "natural",
        campaign_id: str = "", session_id: str = "",
    ) -> Event:
        # 参数错位防护：内容版本必须已登记，且用户标识必须是去标识化伪 ID
        if content_version_id not in self.catalog.versions:
            raise SignalError(
                f"未知内容版本 {content_version_id!r}；请检查 content_version_id/user_pseudo 参数顺序，"
                "或先发布该版本"
            )
        if not user_pseudo.startswith("u_"):
            raise SignalError("user_pseudo 必须是 identity.pseudonymize 产出的伪 ID（u_ 前缀）")
        # 硬门栏：有害内容不得因长期指标实验被重新放大。
        # remove（下架）拦截一切曝光；downrank 至少拦截实验组自然流量（运营投放另受运营策略约束）。
        risk = self.catalog.risk_at(content_version_id, occurred_at)
        if risk.action == "remove":
            raise RiskGateBlocked(
                f"内容 {content_version_id} 已下架（{risk.reason}），任何臂/渠道不得曝光"
            )
        if risk.action == "downrank" and arm == "treatment" and delivery_channel == "natural":
            raise RiskGateBlocked(
                f"内容 {content_version_id} 处于降权处置（{risk.reason}），"
                "treatment 臂自然曝光被风险门栏拦截"
            )
        payload = {
            "exposure_id": exposure_id,
            "content_version_id": content_version_id,
            "user_pseudo": user_pseudo,
            "experiment_id": experiment_id,
            "arm": arm,
            "delivery_channel": delivery_channel,
            "occurred_at": parse_dt(occurred_at).isoformat(),
            "usage_time_segment": usage_time_segment,
        }
        if campaign_id:
            payload["campaign_id"] = campaign_id
        if session_id:
            payload["session_id"] = session_id
        return self.store.append("EXPOSURE_RECORDED", exposure_id, payload,
                                 summary=f"曝光 {content_version_id} → {arm}/{delivery_channel}")

    # ---- 信号 ----
    def record_signal(
        self, exposure: dict, kind: str, signal_ts: str, *,
        provenance: str = "organic", quality_score: float | None = None,
        progress: float | None = None, deleted: bool = False,
        anchor_ts: str | None = None, campaign_id: str = "",
        control_group_id: str = "",
    ) -> Event:
        """exposure 为 EXPOSURE_RECORDED 的 payload。返回 SIGNAL_RECORDED 事件。"""
        self._validate_window(exposure, kind, signal_ts, progress, quality_score, deleted, anchor_ts)
        wid = window_id_for(exposure["exposure_id"], kind)
        signal_ts_iso = parse_dt(signal_ts).isoformat()
        closes_at = self._closes_at(exposure, kind, anchor_ts)
        payload: dict = {
            "window_id": wid,
            "exposure_id": exposure["exposure_id"],
            "content_version_id": exposure["content_version_id"],
            "user_pseudo": exposure["user_pseudo"],
            "experiment_id": exposure["experiment_id"],
            "arm": exposure["arm"],
            "signal_kind": kind,
            "signal_ts": signal_ts_iso,
            "provenance": provenance,
        }
        if anchor_ts:
            payload["anchor_ts"] = parse_dt(anchor_ts).isoformat()
        payload["closes_at"] = closes_at
        if quality_score is not None:
            payload["quality_score"] = float(quality_score)
        if progress is not None:
            payload["progress"] = float(progress)
        if deleted:
            payload["deleted"] = True
        if campaign_id:
            payload["campaign_id"] = campaign_id
        if control_group_id:
            payload["control_group_id"] = control_group_id

        # 迟到判定：到达时间（信封时间）晚于窗口关闭时间
        arrival = self.store.clock.now()
        if arrival > parse_dt(closes_at):
            return self._apply_late(wid, exposure, kind, closes_at, payload, arrival)

        if self.store.version_of("signal_window", wid) > 0:
            raise SignalError(f"窗口 {wid} 已存在信号")
        return self.store.append("SIGNAL_RECORDED", wid, payload,
                                 summary=f"{kind}/{provenance}", occurred_at=arrival.isoformat())

    def _apply_late(self, wid: str, exposure: dict, kind: str, closes_at: str,
                    payload: dict, arrival) -> Event:
        from .experiments import ExperimentService  # 避免循环依赖
        state = WindowReader(self.store).get(wid)
        exp = ExperimentService(self.store).state(exposure["experiment_id"])
        if exp.status == "closed":
            raise LateDataRejected(f"实验 {exposure['experiment_id']} 已封账，迟到 {kind} 不得修正")
        if state is not None and state.finalized:
            raise LedgerFinalized(f"窗口 {wid} 已封账（finalized），迟到数据拒绝修正")
        grace_end = parse_dt(closes_at) + timedelta(hours=GRACE_HOURS)
        if arrival > grace_end:
            raise LateDataRejected(
                f"窗口 {wid} 已过 {GRACE_HOURS}h 迟到宽限（视同封账），{kind} 拒绝修正"
            )
        # 未封账：信号本身照常入账（若窗口已关闭则追加纠正事件）
        if self.store.version_of("signal_window", wid) == 0:
            evt = self.store.append("SIGNAL_RECORDED", wid, payload,
                                    summary=f"迟到 {kind}/{payload['provenance']}")
        else:
            raise LateDataRejected(f"窗口 {wid} 已有信号，迟到重复信号不入账")
        deltas = {kind: {payload["provenance"]: +1}}
        self.store.append(
            "WINDOW_PARTIALLY_CORRECTED", wid,
            {
                "window_id": wid,
                "late_event_ids": [evt.event_id],
                "applied": True,
                "deltas": deltas,
            },
            summary="迟到数据纠正（实验尚未封账）", caused_by=(evt.event_id,),
        )
        return evt

    # ---- 窗口关闭/封账 ----
    def close_due_windows(self, now_iso: str | None = None) -> list[Event]:
        """调度器调用：关闭所有已到 closes_at 的窗口（进入迟到宽限，未封账）。"""
        now = parse_dt(now_iso) if now_iso else self.store.clock.now()
        out: list[Event] = []
        for state in self._all_windows():
            if state.close_event is None and now >= parse_dt(state.closes_at):
                out.append(self._close(state, finalized=False, reason="scheduled"))
        return out

    def finalize_due_windows(self, now_iso: str | None = None) -> list[Event]:
        """宽限期满后封账；封账后任何迟到数据都被拒绝。"""
        now = parse_dt(now_iso) if now_iso else self.store.clock.now()
        out: list[Event] = []
        for state in self._all_windows():
            grace_end = parse_dt(state.closes_at) + timedelta(hours=GRACE_HOURS)
            if state.close_event is not None and not state.finalized and now >= grace_end:
                out.append(self._close(state, finalized=True, reason="scheduled"))
        return out

    def _close(self, state: WindowState, *, finalized: bool, reason: str) -> Event:
        return self.store.append("WINDOW_CLOSED", state.window_id, {
            "window_id": state.window_id,
            "content_version_id": state.content_version_id,
            "experiment_id": state.experiment_id,
            "closes_at": state.closes_at,
            "finalized": finalized,
            "reason": reason,
        }, summary=("封账" if finalized else "窗口关闭，进入迟到宽限"))

    # ---- 再分类：互刷/运营/粉丝回访不得伪装成自然价值 ----
    def reclassify(self, window_id: str, to_provenance: str, reason: str) -> Event:
        state = WindowReader(self.store).get(window_id)
        if state is None or state.signal is None:
            raise SignalError(f"窗口 {window_id} 无信号可再分类")
        if state.finalized:
            raise LedgerFinalized(f"窗口 {window_id} 已封账，再分类需走申诉")
        frm = state.provenance
        if frm == to_provenance:
            raise SignalError("来源未变化")
        evt = self.store.append("SIGNAL_RECLASSIFIED", window_id, {
            "signal_event_id": state.signal["event_id"],
            "window_id": window_id,
            "from_provenance": frm,
            "to_provenance": to_provenance,
            "reason": reason,
        }, summary=f"再分类 {frm} → {to_provenance}：{reason}",
            caused_by=(state.signal["event_id"],))
        return evt

    # ---- 校验 ----
    def _validate_window(self, exposure: dict, kind: str, signal_ts: str,
                         progress: float | None, quality: float | None,
                         deleted: bool, anchor_ts: str | None) -> None:
        if kind not in WINDOW_DEFINITIONS:
            raise SignalError(f"未知信号种类：{kind}")
        cfg = WINDOW_DEFINITIONS[kind]
        t0 = parse_dt(exposure["occurred_at"])
        ts = parse_dt(signal_ts)
        delta = ts - t0
        if delta.total_seconds() < 0:
            raise WindowExpired(f"{kind} 信号早于曝光")
        if delta > timedelta(hours=cfg["max_hours"]):
            raise WindowExpired(
                f"{kind} 超出窗口：曝光后 {delta}，上限 {cfg['max_hours']} 小时"
            )
        if cfg.get("require_next_calendar_day"):
            # 以信号自带时区的日历日判断“跨日”
            if ts.astimezone(t0.tzinfo).date() <= t0.date():
                raise WindowExpired("跨日看完必须发生在曝光次日（含）之后")
            if progress is None or progress < cfg["min_progress"]:
                raise WindowExpired(f"跨日看完需进度 ≥ {cfg['min_progress']}")
        if kind == "effective_discussion":
            if deleted:
                raise SignalError("讨论已删除，不构成有效讨论")
            if quality is None or quality < cfg["min_quality"]:
                raise WindowExpired(f"有效讨论质量分需 ≥ {cfg['min_quality']}")
        if kind == "save_open":
            if not anchor_ts:
                raise SignalError("收藏后打开必须提供收藏时刻 anchor_ts")
            save_ts = parse_dt(anchor_ts)
            if not (t0 <= save_ts <= ts):
                raise WindowExpired("收藏时刻必须位于曝光与打开之间")
            if ts - save_ts > timedelta(hours=cfg["max_hours"]):
                raise WindowExpired(
                    f"收藏后打开超出窗口：收藏后 {ts - save_ts}，上限 {cfg['max_hours']} 小时"
                )

    def _closes_at(self, exposure: dict, kind: str, anchor_ts: str | None) -> str:
        cfg = WINDOW_DEFINITIONS[kind]
        t0 = parse_dt(exposure["occurred_at"])
        if kind == "save_open" and anchor_ts:
            return (parse_dt(anchor_ts) + timedelta(hours=cfg["max_hours"])).isoformat()
        return (t0 + timedelta(hours=cfg["max_hours"])).isoformat()

    def _all_windows(self) -> list[WindowState]:
        return list(WindowReader(self.store).all().values())


class WindowReader:
    """从事件日志重放全部信号窗口状态。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def all(self) -> dict[str, WindowState]:
        states: dict[str, WindowState] = {}
        for evt in self.store.read_all():
            if evt.event_type == "SIGNAL_RECORDED":
                p = evt.payload
                wid = p["window_id"]
                closes_at = p.get("closes_at", p["signal_ts"])
                st = WindowState(
                    window_id=wid, exposure_id=p["exposure_id"],
                    content_version_id=p["content_version_id"],
                    experiment_id=p["experiment_id"], arm=p["arm"],
                    kind=p["signal_kind"], closes_at=closes_at,
                )
                st.signal = {**p, "event_id": evt.event_id}
                st.provenance = p["provenance"]
                states[wid] = st
            elif evt.event_type == "SIGNAL_RECLASSIFIED":
                st = states.get(evt.payload["window_id"])
                if st:
                    st.provenance = evt.payload["to_provenance"]
                    st.signal["provenance"] = evt.payload["to_provenance"]
            elif evt.event_type == "WINDOW_CLOSED":
                st = states.get(evt.payload["window_id"])
                if st is None:
                    # 无信号窗口也可被关闭（边界场景）：占位
                    p = evt.payload
                    st = WindowState(p["window_id"], "", p["content_version_id"],
                                     p["experiment_id"], "", "", p["closes_at"])
                    states[st.window_id] = st
                st.finalized = evt.payload["finalized"]
                st.close_event = evt
            elif evt.event_type == "WINDOW_PARTIALLY_CORRECTED":
                st = states.get(evt.payload["window_id"])
                if st:
                    st.corrections.append(evt.payload)
        return states

    def get(self, window_id: str) -> WindowState | None:
        return self.all().get(window_id)
