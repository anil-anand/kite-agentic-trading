"""Timezone-safe timestamp helpers shared by execution, journal and analytics."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")


def as_utc(value: datetime | str | None) -> datetime | None:
    """Return an aware UTC timestamp.

    Legacy journal records were written without an offset.  They are
    explicitly interpreted as exchange time (IST), never as host-local time.
    """

    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime or ISO timestamp string")
    if value.tzinfo is None:
        value = value.replace(tzinfo=EXCHANGE_TIMEZONE)
    return value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
