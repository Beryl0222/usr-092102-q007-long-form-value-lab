"""追加型事件库与聚合基类。

- 存储为 JSONL，只追加、不修改；复算 = 从头重放。
- event_id 由 (aggregate_type, aggregate_id, version, 规范化 payload) 确定性派生，
  重复提交天然幂等；重放同一日志不会产生新事实。
- 聚合版本乐观并发：append 时校验 version 必须等于该聚合当前版本 + 1。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import EVENT_AGGREGATE, validate_payload
from .identity import stable_hash
from .timekeeping import Clock, parse_dt


class ConcurrentAppendError(RuntimeError):
    pass


class ContractViolation(ValueError):
    pass


def deterministic_event_id(aggregate_type: str, aggregate_id: str, version: int, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"evt_{stable_hash(aggregate_type, aggregate_id, str(version), body)[:24]}"


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    summary: str
    payload: dict = field(default_factory=dict)
    caused_by: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
        }
        if self.caused_by:
            d["caused_by"] = list(self.caused_by)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            event_id=d["event_id"],
            event_type=d["event_type"],
            aggregate_type=d["aggregate_type"],
            aggregate_id=d["aggregate_id"],
            occurred_at=d["occurred_at"],
            version=d["version"],
            summary=d["summary"],
            payload=d.get("payload", {}),
            caused_by=tuple(d.get("caused_by", [])),
        )


class EventStore:
    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or Clock()
        self._versions: dict[str, int] = {}
        self._seen: set[str] = set()
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                evt = Event.from_dict(json.loads(line))
                key = f"{evt.aggregate_type}:{evt.aggregate_id}"
                prev = self._versions.get(key, 0)
                if evt.version != prev + 1:
                    raise RuntimeError(f"日志版本断裂：{key} v{evt.version}（期望 {prev + 1}）")
                self._versions[key] = evt.version
                self._seen.add(evt.event_id)

    def append(
        self,
        event_type: str,
        aggregate_id: str,
        payload: dict | None = None,
        *,
        summary: str = "",
        occurred_at: str | None = None,
        caused_by: tuple[str, ...] = (),
        expected_version: int | None = None,
    ) -> Event:
        if event_type not in EVENT_AGGREGATE:
            raise ContractViolation(f"未登记事件类型：{event_type}")
        aggregate_type = EVENT_AGGREGATE[event_type]
        key = f"{aggregate_type}:{aggregate_id}"
        version = self._versions.get(key, 0) + 1
        if expected_version is not None and expected_version + 1 != version:
            raise ConcurrentAppendError(
                f"{key} 并发冲突：调用方期望基于 v{expected_version}，当前 v{version - 1}"
            )
        errors = validate_payload(event_type, payload)
        if errors:
            raise ContractViolation("；".join(errors))
        payload = payload or {}
        ts = self.clock.now().isoformat() if occurred_at is None else parse_dt(occurred_at).isoformat()
        event_id = deterministic_event_id(aggregate_type, aggregate_id, version, payload)
        if event_id in self._seen:
            raise ConcurrentAppendError(f"重复事件（幂等键已存在）：{event_id}")
        evt = Event(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=ts,
            version=version,
            summary=summary or event_type,
            payload=payload,
            caused_by=tuple(caused_by),
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")
        self._versions[key] = version
        self._seen.add(event_id)
        return evt

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get(f"{aggregate_type}:{aggregate_id}", 0)

    def read_all(self) -> list[Event]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [Event.from_dict(json.loads(line)) for line in fh if line.strip()]

    def read_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[Event]:
        return [
            e for e in self.read_all()
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]


class Aggregate:
    """事件溯源聚合基类：apply 每个事件得到当前状态。"""

    aggregate_type: str = ""

    def __init__(self, aggregate_id: str) -> None:
        self.aggregate_id = aggregate_id
        self.version = 0

    def load(self, events: list[Event]) -> None:
        for evt in events:
            if evt.aggregate_type == self.aggregate_type and evt.aggregate_id == self.aggregate_id:
                self.apply(evt)
                self.version = evt.version

    def apply(self, evt: Event) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError
