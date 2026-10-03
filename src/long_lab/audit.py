"""审计复算：回答“这项策略当初为什么扩大/回滚”。

给定 POLICY_DECIDED 事件：
1. 截取该决策之前的事件序列，重算 manifest_hash，必须与决策记录一致；
2. 用决策锁定的 objective_version 权重重算指标，复现动作与理由；
3. 额外检查事件日志在决策后是否被追加过影响同一实验的纠正（事后可追溯的口径变化）。
"""

from __future__ import annotations

import hashlib
import json

from .catalog import ContentCatalog
from .events import Event, EventStore, deterministic_event_id
from .metrics import replay_events
from .objective import lift, non_organic_share, value_per_exposure


class AuditError(RuntimeError):
    pass


def _hash_prefix(events: list[Event], weights: dict) -> str:
    ids = [e.event_id for e in events]
    body = json.dumps({"events": ids, "weights": weights},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def verify_chain(events: list[Event]) -> None:
    """校验事件内容哈希与聚合版本链；任何对历史 payload/事件的篡改都会在此暴露。"""
    seen: set[str] = set()
    versions: dict[str, int] = {}
    for e in events:
        expected_id = deterministic_event_id(e.aggregate_type, e.aggregate_id, e.version, e.payload)
        if e.event_id != expected_id:
            raise AuditError(f"事件内容被篡改或 event_id 不匹配：{e.event_id}")
        if e.event_id in seen:
            raise AuditError(f"事件重复：{e.event_id}")
        seen.add(e.event_id)
        key = f"{e.aggregate_type}:{e.aggregate_id}"
        if e.version != versions.get(key, 0) + 1:
            raise AuditError(f"版本链断裂：{key} v{e.version}")
        versions[key] = e.version


def recompute_decision(store: EventStore, decision_id: str) -> dict:
    decision = None
    for evt in store.read_aggregate("policy_decision", decision_id):
        if evt.event_type == "POLICY_DECIDED":
            decision = evt
    if decision is None:
        raise AuditError(f"决策不存在：{decision_id}")

    all_events = store.read_all()
    verify_chain(all_events)
    idx = all_events.index(decision)
    # manifest_hash 在决策事件落库前计算，因此只覆盖决策之前的事件序列
    before = all_events[:idx]

    p = decision.payload
    exp_id = p["experiment_id"]
    weights = None
    # 权重锁定到决策记录的 objective_id + objective_version
    objective_id = p.get("objective_id")
    for e in before:
        if (e.event_type == "OBJECTIVE_VERSIONED"
                and (objective_id is None or e.aggregate_id == objective_id)
                and e.payload["objective_version"] == p["objective_version"]):
            weights = e.payload["weights"]
    if weights is None:
        raise AuditError("找不到决策所用目标函数版本")

    recomputed_hash = _hash_prefix(before, weights)
    if recomputed_hash != p["manifest_hash"]:
        raise AuditError(
            f"复算指纹不一致：记录 {p['manifest_hash'][:12]}，重算 {recomputed_hash[:12]}；"
            "日志在决策后被改写或目标权重不匹配"
        )

    catalog = ContentCatalog()
    catalog.load_many(before)
    metrics = replay_events(before, catalog)[exp_id]
    c_val = value_per_exposure(metrics, "control", weights)
    t_val = value_per_exposure(metrics, "treatment", weights)
    observed_lift = lift(metrics, weights)
    nonorg_delta = non_organic_share(metrics, "treatment") - non_organic_share(metrics, "control")
    kill = [e for e in before if e.event_type == "KILL_SWITCH_TRIGGERED"
            and e.payload["experiment_id"] == exp_id]

    return {
        "decision_id": decision_id,
        "experiment_id": exp_id,
        "recorded_action": p["action"],
        "recorded_rationale": p["rationale"],
        "manifest_hash_verified": True,
        "objective_version": p["objective_version"],
        "events_at_decision": idx,
        "recomputed": {
            "value_per_exposure": {"control": round(c_val, 6), "treatment": round(t_val, 6)},
            "lift": round(observed_lift, 6),
            "non_organic_share_delta": round(nonorg_delta, 6),
            "kill_switches": len(kill),
            "finalized_windows": metrics.arms["treatment"].finalized_windows,
        },
        "later_events_on_experiment": [
            {"event_id": e.event_id, "event_type": e.event_type}
            for e in all_events[idx + 1:]
            if e.payload.get("experiment_id") == exp_id
        ],
    }
