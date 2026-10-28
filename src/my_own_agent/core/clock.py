"""Wall-clock snapshots for request context, separate from timeout measurement."""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_clock(clock: Clock) -> datetime:
    value = clock()
    if value.utcoffset() is None:
        raise ValueError("Clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("timezone must be a valid IANA timezone") from error
