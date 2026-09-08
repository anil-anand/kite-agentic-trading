"""Tests for ticker routing (channel + tradingsymbol) and the dev emitter."""

import json
import time

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
