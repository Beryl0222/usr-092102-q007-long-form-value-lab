"""治理：风险止损、机会公平监测、可复算的扩量/回滚决策。

决策门栏顺序不可调换：
1. 风险硬门栏（有害内容曝光率）超标 → 触发 KILL_SWITCH 并暂停分桶，只能 rollback；
2. 公平性门栏（新作者/小众主题/低使用时长人群机会差异）超标 → 禁止 scale；
3. 价值门栏要求提升由 organic 信号解释，非自然来源占比不得异常升高。
每个 POLICY_DECIDED 带 manifest_hash，任何人都能用同一事件序列+目标版本复算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import ContentCatalog
from .events import EventStore
from .experiments import ExperimentService
from .metrics import ExperimentMetrics, manifest_hash, replay
from .objective import ObjectiveRegistry, lift, non_organic_share, value_per_exposure

HARM_RATE_DELTA_MAX = 0.002      # treatment 有害内容曝光率相对 control 的最大允许增量
NON_ORGANIC_DELTA_MAX = 0.05     # 非自然来源占比增量上限：防止“刷出来的提升”
MIN_LIFT_TO_SCALE = 0.03
MIN_FINALIZED_WINDOWS = 100
FAIRNESS_SHARE_DELTA = 0.05      # 各分群在 treatment/control 的曝光占比差上限
FAIRNESS_RATE_RATIO = 0.8        # 弱势群体有机转化率不得低于优势组的 80%
FAIRNESS_SEGMENTS = ("usage_time", "topic", "author_tenure")
PRIORITY_SEGMENTS = {
    "usage_time": "low",
    "topic": "niche",
    "author_tenure": "new",
}


@dataclass
class FairnessReport:
    segments: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {"segments": self.segments, "violations": self.violations}


def fairness_report(m: ExperimentMetrics) -> FairnessReport:
    report = FairnessReport()
    for stype in FAIRNESS_SEGMENTS:
        rows: dict[str, dict] = {}
        for arm in ("control", "treatment"):
            a = m.arms[arm]
            total_expo = a.exposures or 1
            for sval, expo in a.segment_exposures[stype].items():
                org = a.segment_organic[stype].get(sval, 0)
                rows.setdefault(sval, {})[arm] = {
                    "exposure_share": expo / total_expo,
                    "organic_rate": org / expo if expo else 0.0,
                    "exposures": expo,
                }
        report.segments[stype] = rows
        for sval, row in rows.items():
            c, t = row.get("control"), row.get("treatment")
            if not c or not t:
                continue
            if abs(t["exposure_share"] - c["exposure_share"]) > FAIRNESS_SHARE_DELTA:
                report.violations.append(
                    f"{stype}={sval} 曝光占比差异 {t['exposure_share'] - c['exposure_share']:+.3f} 超门栏"
                )
        # 优先关注群体的有机转化率相对比
        focus = PRIORITY_SEGMENTS[stype]
        if focus in rows:
            fr = rows[focus]
            c, t = fr.get("control"), fr.get("treatment")
            if c and t and c["organic_rate"] > 0:
                ratio = t["organic_rate"] / c["organic_rate"]
                if ratio < FAIRNESS_RATE_RATIO:
                    report.violations.append(
                        f"{stype}={focus} treatment 有机转化率仅为 control 的 {ratio:.0%}，低于 {FAIRNESS_RATE_RATIO:.0%}"
                    )
    return report


class GovernanceService:
    def __init__(self, store: EventStore, catalog: ContentCatalog, objective_id: str) -> None:
        self.store = store
        self.catalog = catalog
        self.objective_id = objective_id
        self.experiments = ExperimentService(store)

    def snapshot_fairness(self, experiment_id: str, period_start: str, period_end: str) -> Event:
        metrics = replay(self.store, self.catalog)[experiment_id]
        report = fairness_report(metrics)
        return self.store.append("FAIRNESS_SNAPSHOTTED", experiment_id, {
            "experiment_id": experiment_id,
            "period_start": period_start,
            "period_end": period_end,
            "segments": report.to_payload(),
        }, summary=f"公平性快照：{len(report.violations)} 项差异预警")

    def check_harm(self, experiment_id: str, *, auto_pause: bool = True) -> Event | None:
        """风险止损：treatment 有害内容曝光率增量超门栏即熔断。"""
        metrics = replay(self.store, self.catalog)[experiment_id]
        c, t = metrics.arms["control"], metrics.arms["treatment"]
        c_rate = c.risky_exposures / c.exposures if c.exposures else 0.0
        t_rate = t.risky_exposures / t.exposures if t.exposures else 0.0
        delta = t_rate - c_rate
        if delta <= HARM_RATE_DELTA_MAX:
            return None
        at = self.store.clock.now().isoformat()
        evt = self.store.append("KILL_SWITCH_TRIGGERED", experiment_id, {
            "experiment_id": experiment_id,
            "trigger": "harm_surface_rate",
            "metric": "risky_exposure_rate(treatment)-risky_exposure_rate(control)",
            "threshold": HARM_RATE_DELTA_MAX,
            "observed": round(delta, 6),
            "at": at,
        }, summary=f"紧急止损：有害曝光率增量 {delta:+.4f}")
        if auto_pause:
            exp = self.experiments.state(experiment_id)
            if exp.status == "open":
                self.experiments.pause(experiment_id, "kill_switch", at)
        return evt

    def decide(
        self, experiment_id: str, decision_id: str,
        *, reviewer: str = "", now_iso: str | None = None,
    ) -> Event:
        all_metrics = replay(self.store, self.catalog)
        metrics = all_metrics[experiment_id]
        obj = ObjectiveRegistry(self.store).get(self.objective_id, metrics.objective_version)
        weights = obj.weights

        c_val = value_per_exposure(metrics, "control", weights)
        t_val = value_per_exposure(metrics, "treatment", weights)
        observed_lift = lift(metrics, weights)
        c_nonorg = non_organic_share(metrics, "control")
        t_nonorg = non_organic_share(metrics, "treatment")
        nonorg_delta = t_nonorg - c_nonorg
        report = fairness_report(metrics)

        kill = [
            e.payload for e in self.store.read_aggregate("experiment", experiment_id)
            if e.event_type == "KILL_SWITCH_TRIGGERED"
        ]
        finalized = metrics.arms["treatment"].finalized_windows

        reasons: list[str] = []
        if kill:
            action = "rollback"
            reasons.append(f"已触发紧急止损（{kill[-1]['trigger']}，观测 {kill[-1]['observed']}）")
        elif nonorg_delta > NON_ORGANIC_DELTA_MAX:
            action = "rollback"
            reasons.append(
                f"非自然来源信号占比上升 {nonorg_delta:+.1%}，提升无法由长期价值解释"
            )
        elif report.violations:
            action = "hold"
            reasons.append("公平性门栏未过：" + "；".join(report.violations[:3]))
        elif finalized < MIN_FINALIZED_WINDOWS:
            action = "hold"
            reasons.append(f"已封账窗口 {finalized} < {MIN_FINALIZED_WINDOWS}，证据不足，维持观察")
        elif observed_lift >= MIN_LIFT_TO_SCALE:
            action = "scale"
            reasons.append(
                f"有机价值/曝光 {c_val:.4f}→{t_val:.4f}，提升 {observed_lift:+.1%} ≥ {MIN_LIFT_TO_SCALE:.0%}，"
                f"非自然占比差 {nonorg_delta:+.1%} 在门栏内"
            )
        elif observed_lift <= -MIN_LIFT_TO_SCALE:
            action = "rollback"
            reasons.append(f"有机价值显著下降 {observed_lift:+.1%}")
        else:
            action = "hold"
            reasons.append(f"有机价值变化 {observed_lift:+.1%} 未达扩量/回滚阈值，维持观察")

        snapshot = {
            "value_per_exposure": {"control": round(c_val, 6), "treatment": round(t_val, 6)},
            "lift": round(observed_lift, 6),
            "non_organic_share": {"control": round(c_nonorg, 6), "treatment": round(t_nonorg, 6)},
            "finalized_windows": finalized,
            "fairness_violations": report.violations,
            "kill_switches": len(kill),
            "exposures": {
                arm: {
                    "total": metrics.arms[arm].exposures,
                    "operational": metrics.arms[arm].operational_exposures,
                } for arm in ("control", "treatment")
            },
            "organic_signal_breakdown": {
                arm: {k: v for k, v in metrics.organic_counts(arm).items()}
                for arm in ("control", "treatment")
            },
        }
        h = manifest_hash(self.store, weights)
        return self.store.append("POLICY_DECIDED", decision_id, {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "objective_id": self.objective_id,
            "action": action,
            "manifest_hash": h,
            "objective_version": metrics.objective_version,
            "rationale": "；".join(reasons),
            "metrics_snapshot": snapshot,
            "reviewer": reviewer,
        }, summary=f"决策：{action}（目标 v{metrics.objective_version}，复算指纹 {h[:12]}）")
