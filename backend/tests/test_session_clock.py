from datetime import date, datetime, time, timezone

import pytest

from backend.session_clock import SessionClock, SessionPolicy


def test_clock_uses_exchange_time_not_host_timezone():
    clock = SessionClock(SessionPolicy())

    # 09:20 IST expressed from a UTC host must be an open NSE session.
    snapshot = clock.snapshot(datetime(2026, 9, 21, 3, 50, tzinfo=timezone.utc))

    assert snapshot.exchange_time.hour == 9
    assert snapshot.exchange_time.minute == 20
    assert snapshot.is_open is True
    assert snapshot.entries_allowed is True
    assert snapshot.session_id == "2026-09-21"


def test_clock_handles_holidays_weekends_and_forced_deadline():
    policy = SessionPolicy(
        forced_flatten_time=time(15, 15), holidays=frozenset({date(2026, 9, 22)})
    )
    clock = SessionClock(policy)

    deadline = clock.snapshot(datetime(2026, 9, 21, 9, 46, tzinfo=timezone.utc))
    holiday = clock.snapshot(datetime(2026, 9, 22, 4, 30, tzinfo=timezone.utc))
    weekend = clock.snapshot(datetime(2026, 9, 20, 4, 30, tzinfo=timezone.utc))

    assert deadline.forced_flatten_due is True
    assert deadline.entries_allowed is False
    assert holiday.is_trading_day is False
    assert holiday.is_open is False
    assert weekend.is_trading_day is False
    assert weekend.forced_flatten_due is False


@pytest.mark.parametrize(
    "settings",
    [
        {"squareOffTime": "16:00"},
        {"squareOffTime": "15:15", "noNewTradesAfter": "15:20"},
        {"marketCloseTime": "18:00"},
        {"marketOpenTime": "16:00"},
        {"marketOpenTime": "08:00"},
        {"marketCloseTime": "14:00"},
        {"squareOffTime": None},
    ],
)
def test_invalid_schedule_cannot_extend_intraday_deadlines(settings):
    policy = SessionPolicy.from_risk_config(settings)
    assert policy == SessionPolicy()


def test_direct_policy_never_allows_entries_during_forced_flatten():
    policy = SessionPolicy(no_new_entries_after=time(15, 25))
    snapshot = SessionClock(policy).snapshot(
        datetime(2026, 9, 21, 9, 50, tzinfo=timezone.utc)
    )
    assert snapshot.forced_flatten_due
    assert not snapshot.entries_allowed


def test_close_boundary_is_no_longer_an_open_session():
    snapshot = SessionClock(SessionPolicy()).snapshot(
        datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    )
    assert not snapshot.is_open
    assert snapshot.forced_flatten_due
