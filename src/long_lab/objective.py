"""版本化目标函数。

权重变化必须发 OBJECTIVE_VERSIONED 事件；决策记录所用版本，复算时锁定同一权重。
只把 provenance=organic 的信号计入长期价值——互刷/运营/粉丝集中回访不贡献价值分，
回应“表面提升 vs 长期价值被识别”的质疑。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import EventStore
from .metrics import ExperimentMetrics

# 默认权重：跨日看完与有效讨论权重最高，点击仅作弱信号
DEFAULT_WEIGHTS = {
    "click": 0.05,
    "save": 0.25,
    "save_open": 0.35,
    "cross_day_complete": 0.20,
    "effective_discussion": 0.15,
}


@dataclass
class Objective:
    objective_id: str
    version: int
    weights: dict[str, float]
    notes: str = ""


class ObjectiveRegistry:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def publish(self, objective_id: str, weights: dict[str, float], notes: str = "") -> Objective:
        existing = self.versions(objective_id)
        version = max(existing, default=0) + 1
        self.store.append("OBJECTIVE_VERSIONED", objective_id, {
            "objective_id": objective_id,
            "objective_version": version,
            "weights": weights,
            "notes": notes,
        }, summary=f"目标函数 v{version}")
        return Objective(objective_id, version, dict(weights), notes)

    def versions(self, objective_id: str) -> dict[int, Objective]:
        out: dict[int, Objective] = {}
        for evt in self.store.read_aggregate("objective", objective_id):
            if evt.event_type == "OBJECTIVE_VERSIONED":
                p = evt.payload
                out[p["objective_version"]] = Objective(
                    objective_id, p["objective_version"], p["weights"], p.get("notes", ""))
        return out

    def get(self, objective_id: str, version: int) -> Objective:
        objs = self.versions(objective_id)
        if version not in objs:
            raise KeyError(f"目标函数 {objective_id} v{version} 不存在")
        return objs[version]


def value_per_exposure(m: ExperimentMetrics, arm: str, weights: dict[str, float]) -> float:
    """单位曝光的长期价值（仅有机信号）。"""
    a = m.arms[arm]
    if a.exposures == 0:
        return 0.0
    score = sum(
        n * weights.get(kind, 0.0)
        for (kind, prov), n in a.signals.items() if prov == "organic"
    )
    return score / a.exposures


def non_organic_share(m: ExperimentMetrics, arm: str) -> float:
    """非自然来源信号占比：占比高意味着提升主要来自运营/互刷/粉丝回访。"""
    a = m.arms[arm]
    total = sum(a.signals.values())
    if total == 0:
        return 0.0
    non_org = sum(n for (kind, prov), n in a.signals.items() if prov != "organic")
    return non_org / total


def lift(m: ExperimentMetrics, weights: dict[str, float]) -> float:
    base = value_per_exposure(m, "control", weights)
    if base == 0:
        return 0.0
    return value_per_exposure(m, "treatment", weights) / base - 1.0
