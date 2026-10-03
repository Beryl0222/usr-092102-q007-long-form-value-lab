"""类型化 payload 契约：稳定枚举与负载字段的唯一事实来源（SSOT）。

domain.schema.json 只约束信封；这里登记每种事件的 payload 必填字段与取值域，
tests/test_contract.py 会双向校验两边的事件枚举一致，防止契约漂移。
所有演进只允许 additive：新增可选字段，禁止改老字段语义。
"""

from __future__ import annotations

# ---- 信号种类与各自独立的时间窗（小时为单位，跨日看完按日历日+封顶天数） ----
SIGNAL_KINDS = (
    "click",                  # 普通点击
    "save",                   # 收藏
    "save_open",              # 收藏后打开
    "cross_day_complete",     # 跨日看完
    "effective_discussion",   # 有效讨论
)

# 窗口定义：anchor 为窗口锚点；max_hours 为最长跨度（None 表示由专门规则判定）
WINDOW_DEFINITIONS = {
    # 普通点击：曝光后 1 小时内
    "click": {"anchor": "exposure", "max_hours": 1},
    # 收藏：曝光后 72 小时内
    "save": {"anchor": "exposure", "max_hours": 72},
    # 收藏后打开：以收藏时刻为锚，24 小时内打开
    "save_open": {"anchor": "save", "max_hours": 24},
    # 跨日看完：必须发生在曝光次日（按用户/平台日历日），且曝光后 7 天内
    "cross_day_complete": {"anchor": "exposure", "max_hours": 24 * 7,
                           "require_next_calendar_day": True, "min_progress": 0.9},
    # 有效讨论：曝光后 72 小时内，且达到质量门槛、未被删除
    "effective_discussion": {"anchor": "exposure", "max_hours": 72,
                             "min_quality": 0.6, "require_not_deleted": True},
}

# ---- 信号来源：只有 organic 计入“自然长期价值”，其余单独标记、单独汇总 ----
PROVENANCE = (
    "organic",                  # 自然价值
    "controlled_reciprocal",    # 同一控制关系下的互刷
    "operational_placement",    # 运营投放带来的行为
    "fan_concentrated",         # 粉丝集中回访
)
DELIVERY_CHANNELS = ("natural", "operational_placement")

# ---- 风险处置 ----
RISK_ACTIONS = ("none", "label", "demonetize", "downrank", "remove")
RISK_SOURCES = ("moderation", "anti_rumor", "appeal_review")

# ---- 实验生命周期 ----
EXPERIMENT_STATUSES = ("open", "paused", "closed")
PAUSE_REASONS = ("rule_change", "kill_switch", "manual")
ARMS = ("control", "treatment")

# ---- 决策与申诉 ----
DECISION_ACTIONS = ("hold", "scale", "rollback")
APPEAL_STATUSES = ("accepted", "rejected")

EVENT_AGGREGATE = {
    "CONTENT_VERSION_PUBLISHED": "content_version",
    "CONTENT_VERSION_SUPERSEDED": "content_version",
    "CREATOR_CONTROL_DECLARED": "creator",
    "EXPOSURE_RECORDED": "exposure",
    "SIGNAL_RECORDED": "signal_window",
    "SIGNAL_RECLASSIFIED": "signal_window",
    "WINDOW_CLOSED": "signal_window",
    "WINDOW_PARTIALLY_CORRECTED": "signal_window",
    "EXPERIMENT_OPENED": "experiment",
    "EXPERIMENT_PAUSED": "experiment",
    "EXPERIMENT_RESUMED": "experiment",
    "EXPERIMENT_CLOSED": "experiment",
    "EXPERIMENT_ASSIGNED": "experiment_assignment",
    "OBJECTIVE_VERSIONED": "objective",
    "RISK_ACTION_APPLIED": "content_version",
    "KILL_SWITCH_TRIGGERED": "experiment",
    "FAIRNESS_SNAPSHOTTED": "experiment",
    "POLICY_DECIDED": "policy_decision",
    "APPEAL_DECIDED": "appeal",
}

# payload 字段规格：(字段, 类型, 是否必填, 可选取值域)
# 类型用 Python 类型；取值域用 tuple。bool 需在 int 前判断（单独处理）。
_SPEC = list[tuple[str, type, bool, tuple | None]]


def _spec(*rows: tuple) -> _SPEC:
    return list(rows)


PAYLOAD_CONTRACTS: dict[str, _SPEC] = {
    "CONTENT_VERSION_PUBLISHED": _spec(
        ("content_id", str, True, None),
        ("creator_id", str, True, None),
        ("title", str, True, None),
        ("topic_tags", list, True, None),
        ("is_niche_topic", bool, True, None),
        ("duration_seconds", int, True, None),
        ("content_format", str, False, ("long_video", "article", "audio")),
        ("prev_version_id", str, False, None),
        ("published_at", str, True, None),
    ),
    "CONTENT_VERSION_SUPERSEDED": _spec(
        ("content_id", str, True, None),
        ("superseded_version_id", str, True, None),
        ("new_version_id", str, True, None),
        ("reason", str, True, None),
    ),
    "CREATOR_CONTROL_DECLARED": _spec(
        ("creator_id", str, True, None),
        ("related_creator_ids", list, True, None),
        ("relation", str, True, ("control", "affiliated")),
    ),
    "EXPOSURE_RECORDED": _spec(
        ("exposure_id", str, True, None),
        ("content_version_id", str, True, None),
        ("user_pseudo", str, True, None),
        ("experiment_id", str, True, None),
        ("arm", str, True, ARMS),
        ("delivery_channel", str, True, DELIVERY_CHANNELS),
        ("occurred_at", str, True, None),
        ("usage_time_segment", str, True, ("low", "medium", "high")),
        ("campaign_id", str, False, None),
        ("session_id", str, False, None),
    ),
    "SIGNAL_RECORDED": _spec(
        ("window_id", str, True, None),
        ("exposure_id", str, True, None),
        ("content_version_id", str, True, None),
        ("user_pseudo", str, True, None),
        ("experiment_id", str, True, None),
        ("arm", str, True, ARMS),
        ("signal_kind", str, True, SIGNAL_KINDS),
        ("signal_ts", str, True, None),
        ("provenance", str, True, PROVENANCE),
        ("anchor_ts", str, False, None),
        ("closes_at", str, False, None),
        ("quality_score", float, False, None),
        ("progress", float, False, None),
        ("deleted", bool, False, None),
        ("campaign_id", str, False, None),
        ("control_group_id", str, False, None),
    ),
    "SIGNAL_RECLASSIFIED": _spec(
        ("signal_event_id", str, True, None),
        ("window_id", str, True, None),
        ("from_provenance", str, True, PROVENANCE),
        ("to_provenance", str, True, PROVENANCE),
        ("reason", str, True, None),
    ),
    "WINDOW_CLOSED": _spec(
        ("window_id", str, True, None),
        ("content_version_id", str, True, None),
        ("experiment_id", str, True, None),
        ("closes_at", str, True, None),
        ("finalized", bool, True, None),
        ("reason", str, False, ("scheduled", "manual", "recomputed")),
    ),
    "WINDOW_PARTIALLY_CORRECTED": _spec(
        ("window_id", str, True, None),
        ("late_event_ids", list, True, None),
        ("applied", bool, True, None),
        ("reject_reason", str, False, None),
        ("deltas", dict, False, None),
    ),
    "EXPERIMENT_OPENED": _spec(
        ("experiment_id", str, True, None),
        ("name", str, True, None),
        ("salt", str, True, None),
        ("bucket_weights", dict, True, None),
        ("objective_version", int, True, None),
        ("start_at", str, True, None),
        ("targeting", dict, False, None),
    ),
    "EXPERIMENT_PAUSED": _spec(
        ("experiment_id", str, True, None),
        ("reason", str, True, PAUSE_REASONS),
        ("effective_at", str, True, None),
    ),
    "EXPERIMENT_RESUMED": _spec(
        ("experiment_id", str, True, None),
        ("resumed_at", str, True, None),
        ("salt", str, True, None),
        ("assignment_frozen", bool, True, None),
    ),
    "EXPERIMENT_CLOSED": _spec(
        ("experiment_id", str, True, None),
        ("end_at", str, True, None),
        ("decision_id", str, False, None),
    ),
    "EXPERIMENT_ASSIGNED": _spec(
        ("experiment_id", str, True, None),
        ("user_pseudo", str, True, None),
        ("arm", str, True, ARMS),
        ("bucket", int, True, None),
        ("salt", str, True, None),
        ("assignment_ts", str, True, None),
        ("after_resume", bool, True, None),
    ),
    "OBJECTIVE_VERSIONED": _spec(
        ("objective_id", str, True, None),
        ("objective_version", int, True, None),
        ("weights", dict, True, None),
        ("notes", str, False, None),
    ),
    "RISK_ACTION_APPLIED": _spec(
        ("content_version_id", str, True, None),
        ("action", str, True, RISK_ACTIONS),
        ("reason", str, True, None),
        ("source", str, True, RISK_SOURCES),
        ("effective_at", str, True, None),
    ),
    "KILL_SWITCH_TRIGGERED": _spec(
        ("experiment_id", str, True, None),
        ("trigger", str, True, ("harm_surface_rate", "manual_rule", "risk_action_spike")),
        ("metric", str, True, None),
        ("threshold", float, True, None),
        ("observed", float, True, None),
        ("at", str, True, None),
    ),
    "FAIRNESS_SNAPSHOTTED": _spec(
        ("experiment_id", str, True, None),
        ("period_start", str, True, None),
        ("period_end", str, True, None),
        ("segments", dict, True, None),
    ),
    "POLICY_DECIDED": _spec(
        ("decision_id", str, True, None),
        ("experiment_id", str, True, None),
        ("objective_id", str, True, None),
        ("action", str, True, DECISION_ACTIONS),
        ("manifest_hash", str, True, None),
        ("objective_version", int, True, None),
        ("rationale", str, True, None),
        ("metrics_snapshot", dict, True, None),
        ("reviewer", str, False, None),
    ),
    "APPEAL_DECIDED": _spec(
        ("appeal_id", str, True, None),
        ("content_version_id", str, True, None),
        ("creator_id", str, True, None),
        ("status", str, True, APPEAL_STATUSES),
        ("signal_event_id", str, False, None),
        ("correction_allowed", bool, True, None),
        ("reason", str, True, None),
        ("reviewer", str, True, None),
    ),
}


def validate_payload(event_type: str, payload: dict | None) -> list[str]:
    """对 payload 做轻量结构校验，返回中文错误信息列表（空列表即通过）。"""
    errors: list[str] = []
    spec = PAYLOAD_CONTRACTS.get(event_type)
    if spec is None:
        return [f"未登记的事件类型：{event_type}"]
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return ["payload 必须是对象"]
    for key, typ, required, domain in spec:
        if key not in payload:
            if required:
                errors.append(f"{event_type}.payload 缺少字段：{key}")
            continue
        value = payload[key]
        # bool 是 int 的子类，需要先排除
        if typ is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
            payload[key] = value
        if typ is int and isinstance(value, bool) or not isinstance(value, typ):
            errors.append(f"{event_type}.payload.{key} 类型应为 {typ.__name__}")
            continue
        if domain is not None and value not in domain:
            errors.append(f"{event_type}.payload.{key} 取值非法：{value!r}，允许 {domain}")
    return errors
