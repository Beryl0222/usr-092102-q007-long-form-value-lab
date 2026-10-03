"""测试用场景工厂：固定钟 + 临时事件库 + 组装好的领域服务。"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.long_lab.catalog import ContentCatalog
from src.long_lab.content import ContentService
from src.long_lab.events import EventStore
from src.long_lab.experiments import ExperimentService
from src.long_lab.governance import GovernanceService
from src.long_lab.identity import pseudonymize
from src.long_lab.objective import DEFAULT_WEIGHTS, ObjectiveRegistry
from src.long_lab.signals import SignalPipeline
from src.long_lab.timekeeping import Clock
from src.long_lab.transparency import TransparencyService

START = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


@dataclass
class World:
    tmp: Path
    store: EventStore
    clock: Clock
    content: ContentService
    experiments: ExperimentService
    pipeline: SignalPipeline
    catalog: ContentCatalog
    objectives: ObjectiveRegistry
    objective_id: str

    def refresh_catalog(self) -> ContentCatalog:
        self.catalog = ContentCatalog()
        self.catalog.load_many(self.store.read_all())
        self.pipeline.catalog = self.catalog
        return self.catalog

    def governance(self) -> GovernanceService:
        return GovernanceService(self.store, self.catalog, self.objective_id)

    def transparency(self) -> TransparencyService:
        return TransparencyService(self.store, self.catalog, DEFAULT_WEIGHTS)

    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)
        self.refresh_catalog()


def make_world() -> World:
    tmp = Path(tempfile.mkdtemp(prefix="longlab-"))
    clock = Clock(START)
    store = EventStore(tmp / "events.jsonl", clock=clock)
    content = ContentService(store)
    catalog = ContentCatalog()
    pipeline = SignalPipeline(store, catalog)
    objectives = ObjectiveRegistry(store)
    obj = objectives.publish("obj-long-value", DEFAULT_WEIGHTS, "长内容长期价值 v1")
    return World(
        tmp=tmp, store=store, clock=clock, content=content,
        experiments=ExperimentService(store), pipeline=pipeline,
        catalog=catalog, objectives=objectives, objective_id=obj.objective_id,
    )


def users(n: int, prefix="user") -> list[str]:
    return [pseudonymize(f"{prefix}-{i:04d}") for i in range(n)]


def open_experiment(world: World, exp_id: str = "exp-classic-text", *, salt="s1") -> None:
    world.experiments.open_experiment(
        exp_id, "经典课文长视频长期价值实验", salt,
        {"control": [0], "treatment": [1]}, objective_version=1,
        start_at=world.clock.now().isoformat(),
    )
    world.refresh_catalog()


def publish_classic(world: World, *, vid="cv-laoke-001", creator="creator-laoke",
                    niche=False, published_at=None) -> str:
    published_at = published_at or (world.clock.now() - timedelta(days=60)).isoformat()
    world.content.publish_version(
        vid, "c-laoke", creator, "背影（经典课文长视频）",
        ["语文", "经典课文", "长视频"], niche, 2400,
        published_at=published_at,
    )
    world.refresh_catalog()
    return vid


def make_exposure(world: World, exposure_id: str, user: str, arm: str, vid: str,
                  ts: datetime | str, *, segment: str = "medium",
                  channel: str = "natural", campaign: str = "") -> dict:
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    evt = world.pipeline.record_exposure(
        exposure_id, vid, user, "exp-classic-text", arm, ts,
        segment, delivery_channel=channel, campaign_id=campaign,
    )
    return evt.payload


def add_signal(world: World, expo: dict, kind: str, ts: datetime | str, **kw):
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    defaults = {
        "cross_day_complete": {"progress": 0.95},
        "effective_discussion": {"quality_score": 0.8},
    }
    defaults.setdefault("save", {})
    kw = {**defaults.get(kind, {}), **kw}
    return world.pipeline.record_signal(expo, kind, ts, **kw)


def close_and_finalize_all(world: World) -> None:
    """推进足够久，让所有窗口关闭并封账。"""
    world.advance(days=8)
    world.pipeline.close_due_windows()
    world.advance(hours=25)
    world.pipeline.finalize_due_windows()
