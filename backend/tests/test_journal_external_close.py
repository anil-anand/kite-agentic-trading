"""Tests for journaling a close when a position is closed outside the app-side
exit path (broker-stop fill or a manual close in the Kite app).

Regression: previously the manual-closure path in monitor_positions dropped
tracking without calling journal.close_trade, so broker-stop exits stayed OPEN
forever and never reached analytics.
"""

import datetime

import backend.trading_engine as te
from backend.trading_engine import TradingEngine


class FakeJournal:
    def __init__(self):
        self.closed = []  # (trade_id, exit_price, reason)

    def close_trade(self, trade_id, exit_price, reason):
        self.closed.append((trade_id, exit_price, reason))


class FakeKite:
    def __init__(self, ltp=100.0, orders=None, positions_net=None):
        self._ltp = ltp
        self._orders = orders or []
        self._net = positions_net if positions_net is not None else []

    def get_ltp(self, instruments):
        return {key: {"last_price": self._ltp} for key in instruments}

    def get_orders(self):
        return self._orders

    def get_positions(self):
        return {"net": self._net}

    # monitor_positions touches these too
    def cancel_order(self, *a, **k):
        pass

    def place_order(self, **k):
        return "X"


class FakeRisk:
    def __init__(self):
        self.daily_pnl = 0.0

    def update_pnl(self, pnl):
        pass

    def set_open_positions(self, n):
        pass


def _trade(**over):
    base = {
        "sl": 95.0,
        "target": 110.0,
        "direction": "BUY",
        "entry_price": 100.0,
        "entry_time": datetime.datetime.now(),
        "original_strategy": "test",
        "trade_id": "T1",
        "stop_order_id": "STOP1",
        "exit_pending": False,
        "exit_order_id": None,
        "exchange": "NSE",
    }
    base.update(over)
    return base


class TestJournalExternalClose:
    def _engine(self, monkeypatch, kite, journal):
        monkeypatch.setattr(te, "kite_client", kite)
        monkeypatch.setattr(te, "journal", journal)
        return TradingEngine()

    def test_books_close_with_ltp_and_default_reason(self, monkeypatch):
        j = FakeJournal()
        kite = FakeKite(ltp=97.5, orders=[{"orderId": "STOP1", "status": "OPEN"}])
        eng = self._engine(monkeypatch, kite, j)
        eng.active_trades["RELIANCE"] = _trade()

        eng._journal_external_close("RELIANCE")

        assert j.closed == [("T1", 97.5, "closed_externally")]

    def test_reason_is_stop_loss_when_stop_order_complete(self, monkeypatch):
        j = FakeJournal()
        kite = FakeKite(ltp=95.0, orders=[{"orderId": "STOP1", "status": "COMPLETE"}])
        eng = self._engine(monkeypatch, kite, j)
        eng.active_trades["RELIANCE"] = _trade()

        eng._journal_external_close("RELIANCE")

        assert j.closed == [("T1", 95.0, "stop_loss")]

    def test_noop_when_no_trade_id(self, monkeypatch):
        j = FakeJournal()
        eng = self._engine(monkeypatch, FakeKite(), j)
        eng.active_trades["RELIANCE"] = _trade(trade_id=None)

        eng._journal_external_close("RELIANCE")

        assert j.closed == []

    def test_noop_when_symbol_not_tracked(self, monkeypatch):
        j = FakeJournal()
        eng = self._engine(monkeypatch, FakeKite(), j)

        eng._journal_external_close("MISSING")

        assert j.closed == []

    def test_ltp_failure_still_closes_with_zero(self, monkeypatch):
        j = FakeJournal()

        class Boom(FakeKite):
            def get_ltp(self, instruments):
                raise RuntimeError("network")

        eng = self._engine(monkeypatch, Boom(orders=[]), j)
        eng.active_trades["RELIANCE"] = _trade(stop_order_id=None)

        eng._journal_external_close("RELIANCE")

        assert j.closed == [("T1", 0.0, "closed_externally")]

    def test_monitor_positions_journals_external_closure(self, monkeypatch):
        # End-to-end: a tracked position is no longer in the open book (its
        # broker stop filled) -> monitor_positions must journal the close.
        j = FakeJournal()
        kite = FakeKite(
            ltp=95.0,
            orders=[{"orderId": "STOP1", "status": "COMPLETE"}],
            positions_net=[],  # RELIANCE is flat / gone
        )
        monkeypatch.setattr(te, "kite_client", kite)
        monkeypatch.setattr(te, "journal", j)
        monkeypatch.setattr(te, "risk_manager", FakeRisk())

        eng = TradingEngine()
        eng.active_trades["RELIANCE"] = _trade()

        eng.monitor_positions()

        assert ("T1", 95.0, "stop_loss") in j.closed
        assert "RELIANCE" not in eng.active_trades  # tracking dropped

    def test_stale_position_snapshot_journals_close_before_cleanup(self, monkeypatch):
        # The first snapshot can still contain the position while a protective
        # stop fills before the live re-check in the hit-stop path.
        j = FakeJournal()

        class FlatAfterSnapshotKite(FakeKite):
            def __init__(self):
                super().__init__(
                    ltp=95.0,
                    orders=[{"orderId": "STOP1", "status": "COMPLETE"}],
                    positions_net=[
                        {
                            "tradingsymbol": "RELIANCE",
                            "quantity": 10,
                            "lastPrice": 95.0,
                        }
                    ],
                )
                self.position_reads = 0

            def get_positions(self):
                self.position_reads += 1
                if self.position_reads > 1:
                    return {"net": []}
                return {"net": self._net}

        kite = FlatAfterSnapshotKite()
        monkeypatch.setattr(te, "kite_client", kite)
        monkeypatch.setattr(te, "journal", j)
        monkeypatch.setattr(te, "risk_manager", FakeRisk())

        eng = TradingEngine()
        eng.active_trades["RELIANCE"] = _trade()

        eng.monitor_positions()

        assert ("T1", 95.0, "stop_loss") in j.closed
        assert "RELIANCE" not in eng.active_trades
