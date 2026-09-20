"""Tests for ticker routing (channel + tradingsymbol) and the dev emitter."""

import json
import time
from datetime import datetime, timezone

import pytest

import backend.ticker as tk
from backend.ticker import TickerManager


class FakeMD:
    def get_instruments(self, exchange=None):
        return [
            {"instrument_token": 111, "tradingsymbol": "RELIANCE"},
            {"instrument_token": 222, "tradingsymbol": "INFY"},
        ]

    def get_ltp(self, instruments):
        out = {}
        for key in instruments:
            sym = key.split(":")[-1]
            out[key] = {"last_price": 500.0 if sym == "RELIANCE" else 250.0}
        return out


def _capture_events(capsys):
    events = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


class TestEmitShape:
    def test_emit_uses_renderer_channel_and_tradingsymbol(self, capsys, monkeypatch):
        monkeypatch.setattr(tk, "kite_client", FakeMD())
        mgr = TickerManager()
        mgr._emit_tick(111, 501.25, change_percent=0.25, volume=1000)

        events = _capture_events(capsys)
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "ticker:tick"  # not the old bare "tick"
        d = ev["data"]
        assert d["tradingsymbol"] == "RELIANCE"  # renderer keys ticks by this
        assert d["lastPrice"] == 501.25  # camelCase for the Tick type
        assert d["changePercent"] == 0.25
        assert d["instrumentToken"] == 111
        # Tick type expects a parsable string timestamp, not None.
        import datetime as _dt

        _dt.datetime.fromisoformat(d["timestamp"])

    def test_emit_skips_unknown_token(self, capsys, monkeypatch):
        monkeypatch.setattr(tk, "kite_client", FakeMD())
        mgr = TickerManager()
        mgr._emit_tick(999, 100.0)  # not in the instrument map
        assert _capture_events(capsys) == []

    def test_on_ticks_computes_change_from_prev_close(self, capsys, monkeypatch):
        monkeypatch.setattr(tk, "kite_client", FakeMD())
        mgr = TickerManager()
        mgr.on_ticks(
            None,
            [
                {
                    "instrument_token": 222,
                    "last_price": 255.0,
                    "ohlc": {"close": 250.0},
                    "volume": 5,
                }
            ],
        )
        d = _capture_events(capsys)[0]["data"]
        assert d["tradingsymbol"] == "INFY"
        assert d["changePercent"] == 2.0  # (255-250)/250 * 100

    def test_on_ticks_preserves_source_time_and_records_feed_freshness(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr(tk, "kite_client", FakeMD())
        mgr = TickerManager()
        mgr.on_ticks(
            None,
            [
                {
                    "instrument_token": 222,
                    "last_price": 255.0,
                    "ohlc": {"close": 250.0},
                    "exchange_timestamp": "2026-09-01T10:00:00+05:30",
                }
            ],
        )

        timestamp = _capture_events(capsys)[0]["data"]["timestamp"]
        assert timestamp == "2026-09-01T04:30:00+00:00"
        assert mgr.status()["lastTickAt"] == timestamp

    def test_order_update_uses_renderer_channel(self, capsys):
        mgr = TickerManager()
        mgr.on_order_update(None, {"order_id": "X1", "status": "COMPLETE"})
        ev = _capture_events(capsys)[0]
        assert ev["event"] == "ticker:order-update"


class TestSubscription:
    def test_subscribe_tracks_tokens(self):
        mgr = TickerManager()
        mgr._dev = True  # avoid touching a real websocket
        mgr.subscribe([111, 222, 111])
        assert mgr.tokens == {111, 222}
        mgr.unsubscribe([111])
        assert mgr.tokens == {222}

    def test_unsubscribe_normalizes_stringified_tokens(self):
        # A stringified token from the renderer must still match the int tokens
        # we subscribed with, or the subscription would leak.
        mgr = TickerManager()
        mgr._dev = True
        mgr.subscribe([111, 222])
        mgr.unsubscribe(["111"])
        assert mgr.tokens == {222}

    def test_status_shape(self):
        mgr = TickerManager()
        mgr._dev = True
        mgr.subscribe([111])
        st = mgr.status()
        assert st["tokens"] == 1
        assert "running" in st and "dev" in st


@pytest.mark.parametrize("source_time", [None, "not-a-timestamp"])
def test_unknown_source_time_is_not_reported_as_fresh_market_data(
    source_time, capsys, monkeypatch
):
    monkeypatch.setattr(tk, "kite_client", FakeMD())
    mgr = TickerManager()

    mgr._emit_tick(111, 100, observed_at=source_time)

    event = _capture_events(capsys)[0]["data"]
    assert event["timestampQuality"] == "RECEIPT_ONLY"
    assert event["observedAt"] is None
    assert mgr.status()["lastTickAt"] is None
    assert mgr.status()["lastTickReceivedAt"] == event["receivedAt"]


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_sdk_naive_epoch_timestamp_preserves_instant_on_every_host_timezone(
    host_timezone, capsys, monkeypatch
):
    monkeypatch.setattr(tk, "kite_client", FakeMD())
    # Match the installed SDK's datetime.fromtimestamp wire decoder exactly.
    with monkeypatch.context() as host:
        host.setenv("TZ", host_timezone)
        time.tzset()
        instant = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
        source_time = datetime.fromtimestamp(instant.timestamp())
        mgr = TickerManager()
        mgr.on_ticks(
            None,
            [
                {
                    "instrument_token": 111,
                    "last_price": 100,
                    "exchange_timestamp": source_time,
                    "volume_traded": 123456,
                }
            ],
        )
    time.tzset()

    event = _capture_events(capsys)[0]["data"]
    assert event["timestamp"] == instant.isoformat()
    assert event["volume"] == 123456  # Day-cumulative broker volume, not a tick delta.


def test_out_of_order_ticks_do_not_regress_marks_or_global_freshness(
    capsys, monkeypatch
):
    monkeypatch.setattr(tk, "kite_client", FakeMD())
    mgr = TickerManager()
    newest = "2026-09-01T04:30:00+00:00"
    older = "2026-09-01T04:29:59+00:00"
    mgr._emit_tick(111, 100, observed_at=newest)
    mgr._emit_tick(111, 80, observed_at=older)
    mgr._emit_tick(222, 200, observed_at=older)

    events = _capture_events(capsys)
    assert [event["data"]["lastPrice"] for event in events] == [100, 200]
    assert mgr.status()["lastTickAt"] == newest


def test_future_source_time_cannot_poison_subsequent_tick_freshness(
    capsys, monkeypatch
):
    monkeypatch.setattr(tk, "kite_client", FakeMD())
    instant = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(tk, "now_utc", lambda: instant)
    mgr = TickerManager()
    mgr._emit_tick(111, 120, observed_at="2026-09-02T04:30:00+00:00")
    mgr._emit_tick(111, 100, observed_at=instant)

    assert [event["data"]["lastPrice"] for event in _capture_events(capsys)] == [100]
    assert mgr.status()["lastTickAt"] == instant.isoformat()


def test_malformed_price_or_token_does_not_stop_remaining_batch(capsys, monkeypatch):
    monkeypatch.setattr(tk, "kite_client", FakeMD())
    mgr = TickerManager()
    mgr.on_ticks(
        None,
        [
            {"instrument_token": 111, "last_price": "bad"},
            {"instrument_token": None, "last_price": 100},
            {"instrument_token": 222, "last_price": 200},
        ],
    )

    assert [event["data"]["lastPrice"] for event in _capture_events(capsys)] == [200]


class TestDevEmitter:
    def test_dev_start_runs_emitter_and_emits_for_subscribed(self, capsys, monkeypatch):
        monkeypatch.setattr(tk, "kite_client", FakeMD())
        monkeypatch.setattr(tk, "is_dev_mode", lambda: True)
        # Speed the loop up so the test is fast.
        real_sleep = time.sleep
        monkeypatch.setattr(tk.time, "sleep", lambda s: real_sleep(0.02))

        mgr = TickerManager()
        mgr.start("dev", "dev")
        try:
            assert mgr._dev is True
            mgr.subscribe([111])
            real_sleep(0.1)  # let the emitter tick a few times
            events = _capture_events(capsys)
            assert any(
                e.get("event") == "ticker:tick"
                and e["data"]["tradingsymbol"] == "RELIANCE"
                for e in events
            )
        finally:
            mgr.stop()

    def test_dev_start_does_not_open_websocket(self, monkeypatch):
        monkeypatch.setattr(tk, "is_dev_mode", lambda: True)
        mgr = TickerManager()
        mgr.start("dev", "dev")
        try:
            assert mgr.ticker is None  # no KiteTicker in dev
            assert mgr.running is True
        finally:
            mgr.stop()
