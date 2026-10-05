"""Exchange-session timekeeping with an explicit, injectable clock.

This module deliberately does not use the host timezone or ``datetime.now``.
Callers provide the event time, which makes session decisions replayable and
keeps a process running across midnight from silently resetting risk state.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import FrozenSet, Iterable

from .time_utils import EXCHANGE_TIMEZONE, as_utc


def parse_exchange_time(value: object, default: str) -> time:
    """Parse a configured HH:MM value, falling back to a safe default."""

    candidate = value if isinstance(value, str) else default
    try:
        return datetime.strptime(candidate, "%H:%M").time()
    except ValueError:
        return datetime.strptime(default, "%H:%M").time()


@dataclass(frozen=True)
class SessionPolicy:
    """Versioned NSE cash-session policy used by hard-risk supervision."""

    policy_version: str = "session-clock-v1"
    exchange_timezone: object = EXCHANGE_TIMEZONE
    open_time: time = time(9, 15)
    close_time: time = time(15, 30)
    no_new_entries_after: time = time(15, 0)
    forced_flatten_time: time = time(15, 15)
    holidays: FrozenSet[date] = frozenset()

    @classmethod
    def from_risk_config(
        cls, risk_config: dict, *, holidays: Iterable[date] = ()
    ) -> "SessionPolicy":
        policy = cls(
            open_time=parse_exchange_time(risk_config.get("marketOpenTime"), "09:15"),
            close_time=parse_exchange_time(risk_config.get("marketCloseTime"), "15:30"),
            no_new_entries_after=parse_exchange_time(
                risk_config.get("noNewTradesAfter"), "15:00"
            ),
            forced_flatten_time=parse_exchange_time(
                risk_config.get("squareOffTime"), "15:15"
            ),
            holidays=frozenset(holidays),
        )
        # Invalid settings must not move a mandatory intraday close beyond
        # the exchange session or let entries race the flatten deadline.
        if not (
            time(9, 15)
            <= policy.open_time
            < policy.no_new_entries_after
            <= policy.forced_flatten_time
            < policy.close_time
            <= time(15, 30)
        ):
            return cls(holidays=policy.holidays)
        return policy


@dataclass(frozen=True)
class SessionSnapshot:
    """The complete exchange-session decision for one explicit instant."""

    observed_at: datetime
    exchange_time: datetime
    session_date: date
    is_trading_day: bool
    is_open: bool
    entries_allowed: bool
    forced_flatten_due: bool
    after_close: bool
    policy_version: str

    @property
    def session_id(self) -> str:
        return self.session_date.isoformat()


class SessionClock:
    """Converts explicit UTC/exchange event timestamps into session facts."""

    def __init__(self, policy: SessionPolicy):
        self.policy = policy

    def snapshot(self, observed_at: datetime) -> SessionSnapshot:
        utc_time = as_utc(observed_at)
        if utc_time is None:
            raise ValueError("session clock requires an explicit event timestamp")
        exchange_time = utc_time.astimezone(self.policy.exchange_timezone)
        session_date = exchange_time.date()
        is_trading_day = (
            exchange_time.weekday() < 5 and session_date not in self.policy.holidays
        )
        current_time = exchange_time.time()
        is_open = (
            is_trading_day
            and self.policy.open_time <= current_time < self.policy.close_time
        )
        return SessionSnapshot(
            observed_at=utc_time,
            exchange_time=exchange_time,
            session_date=session_date,
            is_trading_day=is_trading_day,
            is_open=is_open,
            entries_allowed=is_open
            and current_time
            < min(self.policy.no_new_entries_after, self.policy.forced_flatten_time),
            forced_flatten_due=is_trading_day
            and current_time >= self.policy.forced_flatten_time,
            after_close=is_trading_day and current_time > self.policy.close_time,
            policy_version=self.policy.policy_version,
        )
