"""指标投影：从事件日志完全重放的读模型。

任何分析结论都必须能由 replay() 重新得到；策略决策记录 manifest_hash，
事后可用同一批事件与同一 objective_version 复算，解释“为何扩大或回滚”。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

from .catalog import ContentCatalog
from .events import EventStore


@dataclass
class ArmMetrics:
    exposures: int = 0
    operational_exposures: int = 0
    signals: dict = field(default_factory=lambda: defaultdict(int))            # (kind, provenance)
    organic_value_components: dict = field(default_factory=lambda: defaultdict(float))
    late_corrections: int = 0
    risk_gated_blocked: int = 0
    reclassified: list[dict] = field(default_factory=list)
    # 分群曝光计数：segment_type -> segment_value -> n
    segment_exposures: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    # 分群有机价值信号数
    segment_organic: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    # 组合准标识符去重用户（外部报告 k-匿名抑制需要）：(usage, topic, tenure) -> users
    combo_users: dict = field(default_factory=lambda: defaultdict(set))
    # 按内容版本
    per_content_organic: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    finalized_windows: int = 0
    risky_exposures: int = 0  # 曝光当时内容处于任意风险处置（label 及以上）


@dataclass
class ExperimentMetrics:
    experiment_id: str
    arms: dict[str, ArmMetrics] = field(default_factory=lambda: {"control": ArmMetrics(), "treatment": ArmMetrics()})
    objective_version: int = 0
    event_count: int = 0

    def organic_counts(self, arm: str) -> dict[str, int]:
        return {
            kind: n for (kind, prov), n in self.arms[arm].signals.items()
            if prov == "organic"
        }

    def organic_exposure_rate(self, arm: str) -> float:
        m = self.arms[arm]
        if m.exposures == 0:
            return 0.0
        return sum(self.organic_counts(arm).values()) / m.exposures


def replay(store: EventStore, catalog: ContentCatalog | None = None) -> dict[str, ExperimentMetrics]:
    """重放全部事件，产出 {experiment_id: ExperimentMetrics}。纯函数式、无副作用。"""
    return replay_events(store.read_all(), catalog)


def replay_events(events: list, catalog: ContentCatalog | None = None) -> dict[str, ExperimentMetrics]:
    """按给定事件序列重放（复算决策时用于截取决策时点之前的事件）。"""
    if catalog is None:
        catalog = ContentCatalog()
        catalog.load_many(events)
    win_map = _replay_windows(events)
    expo_payloads = {
        e.aggregate_id: e.payload for e in events if e.event_type == "EXPOSURE_RECORDED"
    }
    out: dict[str, ExperimentMetrics] = {}

    def em(experiment_id: str) -> ExperimentMetrics:
        if experiment_id not in out:
            out[experiment_id] = ExperimentMetrics(experiment_id=experiment_id)
        return out[experiment_id]

    for evt in events:
        p = evt.payload
        if evt.event_type == "EXPERIMENT_OPENED":
            em(p["experiment_id"]).objective_version = p["objective_version"]
        elif evt.event_type == "EXPOSURE_RECORDED":
            m = em(p["experiment_id"]); a = m.arms[p["arm"]]
            a.exposures += 1
            if p["delivery_channel"] == "operational_placement":
                a.operational_exposures += 1
            seg = _segments(p, catalog)
            for stype, sval in seg.items():
                a.segment_exposures[stype][sval] += 1
            if len(seg) == 3:
                combo = (seg.get("usage_time", "?"), seg.get("topic", "?"), seg.get("author_tenure", "?"))
                a.combo_users[combo].add(p["user_pseudo"])
            if catalog.risk_at(p["content_version_id"], p["occurred_at"]).action != "none":
                a.risky_exposures += 1
            m.event_count += 1
        elif evt.event_type == "SIGNAL_RECORDED":
            m = em(p["experiment_id"]); a = m.arms[p["arm"]]
            a.signals[(p["signal_kind"], p["provenance"])] += 1
            if p["provenance"] == "organic":
                a.per_content_organic[p["content_version_id"]][p["signal_kind"]] += 1
                expo = expo_payloads.get(p["exposure_id"], {})
                for stype, sval in _segments(expo, catalog).items():
                    a.segment_organic[stype][sval] += 1
        elif evt.event_type == "SIGNAL_RECLASSIFIED":
            win = win_map.get(p["window_id"])
            if win:
                m = em(win.experiment_id); a = m.arms[win.arm]
                a.signals[(win.kind, p["from_provenance"])] -= 1
                a.signals[(win.kind, p["to_provenance"])] += 1
                a.reclassified.append(p)
        elif evt.event_type == "WINDOW_PARTIALLY_CORRECTED":
            win = win_map.get(p["window_id"])
            if win and p.get("applied"):
                em(win.experiment_id).arms[win.arm].late_corrections += 1
        elif evt.event_type == "WINDOW_CLOSED" and p.get("finalized"):
            win = win_map.get(p["window_id"])
            if win and win.signal is not None:
                em(win.experiment_id).arms[win.arm].finalized_windows += 1
    return out


def _replay_windows(events: list) -> dict:
    from .signals import WindowState
    states: dict[str, WindowState] = {}
    for evt in events:
        if evt.event_type == "SIGNAL_RECORDED":
            p = evt.payload
            st = WindowState(
                window_id=p["window_id"], exposure_id=p["exposure_id"],
                content_version_id=p["content_version_id"], experiment_id=p["experiment_id"],
                arm=p["arm"], kind=p["signal_kind"], closes_at=p.get("closes_at", p["signal_ts"]),
            )
            st.signal = {**p, "event_id": evt.event_id}
            st.provenance = p["provenance"]
            states[st.window_id] = st
        elif evt.event_type == "SIGNAL_RECLASSIFIED":
            st = states.get(evt.payload["window_id"])
            if st:
                st.provenance = evt.payload["to_provenance"]
                st.signal["provenance"] = evt.payload["to_provenance"]
        elif evt.event_type == "WINDOW_CLOSED":
            st = states.get(evt.payload["window_id"])
            if st is not None:
                st.finalized = evt.payload["finalized"]
                st.close_event = evt
        elif evt.event_type == "WINDOW_PARTIALLY_CORRECTED":
            st = states.get(evt.payload["window_id"])
            if st:
                st.corrections.append(evt.payload)
    return states


def manifest_hash(store: EventStore, objective_weights: dict, *, up_to_event_id: str | None = None) -> str:
    """决策复算指纹：规范化全部事件 id（有序）+ 目标权重。

    审查人员事后可凭该哈希验证：用同一事件序列与同一权重重算，必然得到同一结论。
    """
    ids = [e.event_id for e in store.read_all()]
    if up_to_event_id is not None:
        ids = ids[: ids.index(up_to_event_id) + 1]
    body = json.dumps({"events": ids, "weights": objective_weights},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _segments(exposure_payload: dict, catalog: ContentCatalog) -> dict[str, str]:
    if not exposure_payload:
        return {}
    seg = {"usage_time": exposure_payload.get("usage_time_segment", "unknown")}
    cv = catalog.versions.get(exposure_payload.get("content_version_id", ""))
    if cv is not None:
        seg["topic"] = "niche" if cv.is_niche_topic else "mainstream"
        seg["author_tenure"] = (
            "new" if catalog.is_new_author(cv.creator_id, exposure_payload["occurred_at"]) else "established"
        )
    return seg
