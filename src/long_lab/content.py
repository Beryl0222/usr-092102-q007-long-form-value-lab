"""内容服务：发布版本、声明控制关系、风险处置。"""

from __future__ import annotations

from .events import Event, EventStore


class ContentService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    def publish_version(
        self, content_version_id: str, content_id: str, creator_id: str, title: str,
        topic_tags: list[str], is_niche_topic: bool, duration_seconds: int,
        published_at: str | None = None, *, content_format: str = "long_video",
        prev_version_id: str = "",
    ) -> Event:
        payload: dict = {
            "content_id": content_id,
            "creator_id": creator_id,
            "title": title,
            "topic_tags": topic_tags,
            "is_niche_topic": is_niche_topic,
            "duration_seconds": duration_seconds,
            "published_at": published_at or self.store.clock.now().isoformat(),
            "content_format": content_format,
        }
        if prev_version_id:
            payload["prev_version_id"] = prev_version_id
        return self.store.append("CONTENT_VERSION_PUBLISHED", content_version_id, payload,
                                 summary=f"发布 {title}", occurred_at=payload["published_at"])

    def supersede(self, content_id: str, old_version_id: str, new_version_id: str, reason: str) -> Event:
        return self.store.append("CONTENT_VERSION_SUPERSEDED", old_version_id, {
            "content_id": content_id,
            "superseded_version_id": old_version_id,
            "new_version_id": new_version_id,
            "reason": reason,
        }, summary=f"版本更替：{reason}")

    def declare_control(self, creator_id: str, related_creator_ids: list[str], relation: str = "control") -> Event:
        return self.store.append("CREATOR_CONTROL_DECLARED", creator_id, {
            "creator_id": creator_id,
            "related_creator_ids": related_creator_ids,
            "relation": relation,
        }, summary=f"声明{relation}关系：{creator_id} ↔ {related_creator_ids}")

    def apply_risk_action(
        self, content_version_id: str, action: str, reason: str,
        source: str = "moderation", effective_at: str | None = None,
    ) -> Event:
        return self.store.append("RISK_ACTION_APPLIED", content_version_id, {
            "content_version_id": content_version_id,
            "action": action,
            "reason": reason,
            "source": source,
            "effective_at": effective_at or self.store.clock.now().isoformat(),
        }, summary=f"风险处置 {action}（{source}：{reason}）")
