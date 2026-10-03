"""实验生命周期与确定性分桶。

关键不变量：
1. 暂停（规则变更/紧急止损）期间拒绝一切新分桶；
2. 恢复必须沿用开轮盐（assignment_frozen），分桶函数对同一用户确定性输出同一桶，
   因此老用户不可能跨组；新用户用 after_resume 标记，分析时单独报告；
3. 任何历史上同实验同用户出现不同 arm 的记录都构成污染，审计器会报出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import ARMS
from .events import Event, EventStore
from .identity import deterministic_bucket


class ExperimentError(RuntimeError):
    pass


class AssignmentBlocked(ExperimentError):
    """实验暂停期间不允许分桶。"""


@dataclass
class ExperimentState:
    experiment_id: str
    name: str = ""
    salt: str = ""
    bucket_weights: dict[str, list[int]] = field(default_factory=dict)
    objective_version: int = 0
    start_at: str = ""
    status: str = "open"
    targeting: dict = field(default_factory=dict)
    pause_reason: str | None = None
    paused_at: str | None = None
    resumed_at: str | None = None
    end_at: str | None = None
    kill_triggers: list[dict] = field(default_factory=list)


class ExperimentService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ---- 生命周期 ----
    def open_experiment(
        self, experiment_id: str, name: str, salt: str,
        bucket_weights: dict[str, list[int]], objective_version: int,
        start_at: str | None = None, targeting: dict | None = None,
    ) -> Event:
        if self.store.version_of("experiment", experiment_id):
            raise ExperimentError(f"实验已存在：{experiment_id}")
        self._validate_weights(bucket_weights)
        return self.store.append(
            "EXPERIMENT_OPENED", experiment_id,
            {
                "experiment_id": experiment_id,
                "name": name,
                "salt": salt,
                "bucket_weights": bucket_weights,
                "objective_version": objective_version,
                "start_at": start_at or self.store.clock.now().isoformat(),
                "targeting": targeting or {},
            },
            summary=f"开轮：{name}",
        )

    def pause(self, experiment_id: str, reason: str, effective_at: str | None = None) -> Event:
        exp = self.state(experiment_id)
        if exp.status != "open":
            raise ExperimentError(f"仅 open 实验可暂停，当前 {exp.status}")
        return self.store.append("EXPERIMENT_PAUSED", experiment_id, {
            "experiment_id": experiment_id,
            "reason": reason,
            "effective_at": effective_at or self.store.clock.now().isoformat(),
        }, summary=f"暂停分桶：{reason}")

    def resume(self, experiment_id: str, resumed_at: str | None = None, salt: str | None = None) -> Event:
        exp = self.state(experiment_id)
        if exp.status != "paused":
            raise ExperimentError(f"仅 paused 实验可恢复，当前 {exp.status}")
        # 强制沿用原盐：换盐会让老用户重哈希到别的组，造成跨组污染
        chosen_salt = salt or exp.salt
        if chosen_salt != exp.salt:
            raise ExperimentError("恢复时禁止更换分桶盐（会造成跨组污染）；新轮次请另开实验")
        return self.store.append("EXPERIMENT_RESUMED", experiment_id, {
            "experiment_id": experiment_id,
            "resumed_at": resumed_at or self.store.clock.now().isoformat(),
            "salt": chosen_salt,
            "assignment_frozen": True,
        }, summary="恢复分桶，老用户分配冻结")

    def close(self, experiment_id: str, decision_id: str | None = None) -> Event:
        exp = self.state(experiment_id)
        if exp.status == "closed":
            raise ExperimentError("实验已结束")
        return self.store.append("EXPERIMENT_CLOSED", experiment_id, {
            "experiment_id": experiment_id,
            "end_at": self.store.clock.now().isoformat(),
            "decision_id": decision_id or "",
        }, summary="结束实验")

    # ---- 分桶 ----
    def assign(self, experiment_id: str, user_pseudo: str, assignment_ts: str | None = None) -> Event:
        exp = self.state(experiment_id)
        if exp.status == "paused":
            raise AssignmentBlocked(
                f"{experiment_id} 因 {exp.pause_reason} 暂停，拒绝分桶（规则变更/止损期间不产生新分配）"
            )
        if exp.status == "closed":
            raise AssignmentBlocked("实验已结束，拒绝分桶")
        n = max(max(v) for v in exp.bucket_weights.values()) + 1
        bucket = deterministic_bucket(user_pseudo, experiment_id, exp.salt, n)
        arm = self._arm_for(exp, bucket)
        ts = assignment_ts or self.store.clock.now().isoformat()
        # 幂等：该用户已有等价分配则直接返回原事件；不同 arm 则是污染
        existing = self.assignments_for(experiment_id).get(user_pseudo)
        if existing is not None:
            if existing["arm"] != arm:
                raise ExperimentError(
                    f"跨组污染：{user_pseudo} 历史 {existing['arm']}，现计算为 {arm}"
                )
            return existing["event"]
        return self.store.append("EXPERIMENT_ASSIGNED", f"{experiment_id}:{user_pseudo}", {
            "experiment_id": experiment_id,
            "user_pseudo": user_pseudo,
            "arm": arm,
            "bucket": bucket,
            "salt": exp.salt,
            "assignment_ts": ts,
            "after_resume": exp.resumed_at is not None and ts >= exp.resumed_at,
        }, summary=f"分配 {arm}（桶 {bucket}）")

    # ---- 读模型 ----
    def state(self, experiment_id: str) -> ExperimentState:
        state = ExperimentState(experiment_id=experiment_id)
        found = False
        for evt in self.store.read_aggregate("experiment", experiment_id):
            p = evt.payload
            found = True
            if evt.event_type == "EXPERIMENT_OPENED":
                state.name = p["name"]; state.salt = p["salt"]
                state.bucket_weights = p["bucket_weights"]
                state.objective_version = p["objective_version"]
                state.start_at = p["start_at"]; state.targeting = p.get("targeting", {})
            elif evt.event_type == "EXPERIMENT_PAUSED":
                state.status = "paused"; state.pause_reason = p["reason"]
                state.paused_at = p["effective_at"]
            elif evt.event_type == "EXPERIMENT_RESUMED":
                state.status = "open"; state.resumed_at = p["resumed_at"]
                state.salt = p["salt"]
            elif evt.event_type == "EXPERIMENT_CLOSED":
                state.status = "closed"; state.end_at = p["end_at"]
            elif evt.event_type == "KILL_SWITCH_TRIGGERED":
                state.kill_triggers.append(p)
        if not found:
            raise ExperimentError(f"实验不存在：{experiment_id}")
        return state

    def assignments_for(self, experiment_id: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for evt in self.store.read_all():
            if evt.event_type == "EXPERIMENT_ASSIGNED" and evt.payload["experiment_id"] == experiment_id:
                p = evt.payload
                out[p["user_pseudo"]] = {"arm": p["arm"], "bucket": p["bucket"], "event": evt}
        return out

    @staticmethod
    def _arm_for(exp: ExperimentState, bucket: int) -> str:
        for arm in ARMS:
            if bucket in exp.bucket_weights.get(arm, []):
                return arm
        raise ExperimentError(f"桶 {bucket} 未映射到任何组")

    @staticmethod
    def _validate_weights(weights: dict[str, list[int]]) -> None:
        if set(weights) != set(ARMS):
            raise ExperimentError(f"bucket_weights 必须恰好包含 {ARMS}")
        seen: list[int] = []
        for buckets in weights.values():
            if not buckets:
                raise ExperimentError("每个组至少占一个桶")
            seen.extend(buckets)
        if len(seen) != len(set(seen)):
            raise ExperimentError("桶不能跨组重复")


def audit_contamination(store: EventStore) -> list[dict]:
    """全局审计：同实验同用户出现不同 arm/桶/盐即为污染。"""
    seen: dict[tuple[str, str], dict] = {}
    violations: list[dict] = []
    for evt in store.read_all():
        if evt.event_type != "EXPERIMENT_ASSIGNED":
            continue
        p = evt.payload
        key = (p["experiment_id"], p["user_pseudo"])
        prev = seen.get(key)
        if prev is not None and (prev["arm"] != p["arm"] or prev["bucket"] != p["bucket"] or prev["salt"] != p["salt"]):
            violations.append({
                "experiment_id": p["experiment_id"],
                "user_pseudo": p["user_pseudo"],
                "first": prev["event_id"],
                "conflicting": evt.event_id,
            })
        else:
            seen[key] = {"arm": p["arm"], "bucket": p["bucket"], "salt": p["salt"], "event_id": evt.event_id}
    return violations
