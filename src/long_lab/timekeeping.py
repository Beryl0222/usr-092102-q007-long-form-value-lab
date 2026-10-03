"""可注入时钟：测试用固定钟，生产用 UTC 系统钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def advance(self, **kwargs) -> None:
        assert self._fixed is not None, "固定钟才能推进"
        self._fixed = self._fixed + timedelta(**kwargs)


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(timezone.utc)
