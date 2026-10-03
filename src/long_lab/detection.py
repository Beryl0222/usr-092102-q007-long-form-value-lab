"""非自然来源判定：把可疑行为在入库时就标成独立 provenance。

这些判定只改变“归类”，不丢弃行为：分析师仍能看到运营/互刷/粉丝回访的量级，
但它们绝不进入长期价值分，也不会被包装成自然增长。
"""

from __future__ import annotations

from .catalog import ContentCatalog


def detect_provenance(
    exposure: dict, catalog: ContentCatalog, content_creator_id: str,
    *, viewer_creator_id: str | None = None, is_known_fan: bool = False,
) -> str:
    if exposure.get("delivery_channel") == "operational_placement" or exposure.get("campaign_id"):
        return "operational_placement"
    if viewer_creator_id is not None and catalog.same_control(viewer_creator_id, content_creator_id):
        return "controlled_reciprocal"
    if is_known_fan:
        return "fan_concentrated"
    return "organic"
