"""内容目录读模型：内容版本、创作者控制关系（并查集）、风险处置时间线。

全部状态由事件重放得到；控制关系用于识别“同一控制关系下的互刷”，
风险时间线保证有害内容不会因长期指标被重新放大。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import Aggregate, Event


@dataclass
class ContentVersion:
    content_version_id: str
    content_id: str
    creator_id: str
    title: str
    topic_tags: list[str]
    is_niche_topic: bool
    duration_seconds: int
    published_at: str
    content_format: str = "long_video"
    superseded_by: str | None = None


@dataclass
class RiskState:
    action: str = "none"
    source: str | None = None
    reason: str | None = None
    effective_at: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def is_gated(self) -> bool:
        """当前是否处于硬门栏处置（降权/下架不得被长期指标重新放大）。"""
        return self.action in ("downrank", "remove")


class _DSU:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def union(self, a: str, b: str) -> None:
        self.add(a); self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root


class ContentCatalog(Aggregate):
    aggregate_type = "content_version"  # 重放时按事件类型过滤，见 load_many

    def __init__(self) -> None:
        super().__init__("__catalog__")
        self.versions: dict[str, ContentVersion] = {}
        self.by_content: dict[str, list[str]] = {}
        self.creator_first_publish: dict[str, str] = {}
        self.control = _DSU()
        self.risk: dict[str, RiskState] = {}

    def load_many(self, events: list[Event]) -> None:
        for evt in events:
            self.apply(evt)

    def apply(self, evt: Event) -> None:
        p = evt.payload
        if evt.event_type == "CONTENT_VERSION_PUBLISHED":
            vid = evt.aggregate_id
            self.versions[vid] = ContentVersion(
                content_version_id=vid,
                content_id=p["content_id"],
                creator_id=p["creator_id"],
                title=p["title"],
                topic_tags=list(p["topic_tags"]),
                is_niche_topic=bool(p["is_niche_topic"]),
                duration_seconds=p["duration_seconds"],
                published_at=p["published_at"],
                content_format=p.get("content_format", "long_video"),
            )
            self.by_content.setdefault(p["content_id"], []).append(vid)
            prev = self.creator_first_publish.get(p["creator_id"])
            if prev is None or p["published_at"] < prev:
                self.creator_first_publish[p["creator_id"]] = p["published_at"]
        elif evt.event_type == "CONTENT_VERSION_SUPERSEDED":
            old = self.versions.get(p["superseded_version_id"])
            if old is not None:
                old.superseded_by = p["new_version_id"]
        elif evt.event_type == "CREATOR_CONTROL_DECLARED":
            # control 关系并入同一控制组；affiliated 仅标注、不并组
            if p["relation"] == "control":
                for other in p["related_creator_ids"]:
                    self.control.union(p["creator_id"], other)
        elif evt.event_type == "RISK_ACTION_APPLIED":
            vid = p["content_version_id"]
            state = self.risk.setdefault(vid, RiskState())
            state.action = p["action"]
            state.source = p["source"]
            state.reason = p["reason"]
            state.effective_at = p["effective_at"]
            state.history.append({
                "action": p["action"], "source": p["source"],
                "reason": p["reason"], "effective_at": p["effective_at"],
                "event_id": evt.event_id,
            })

    # ---- 查询 ----
    def control_group(self, creator_id: str) -> str:
        return self.control.find(creator_id)

    def same_control(self, a: str, b: str) -> bool:
        return self.control.find(a) == self.control.find(b)

    def risk_at(self, content_version_id: str, at_iso: str) -> RiskState:
        """重放给定时刻有效的风险处置（用于判定曝光当时是否已被门栏）。"""
        state = RiskState()
        for item in self.risk.get(content_version_id, RiskState()).history:
            if item["effective_at"] <= at_iso:
                state.action = item["action"]
                state.source = item["source"]
                state.reason = item["reason"]
                state.effective_at = item["effective_at"]
        return state

    def is_new_author(self, creator_id: str, as_of_iso: str, window_days: int = 30) -> bool:
        first = self.creator_first_publish.get(creator_id)
        if first is None:
            return False
        from .timekeeping import parse_dt
        from datetime import timedelta
        return parse_dt(as_of_iso) - parse_dt(first) <= timedelta(days=window_days)
