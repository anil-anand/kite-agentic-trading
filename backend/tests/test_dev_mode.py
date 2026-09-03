"""Tests for KITE_DEV_MODE: the flag, the mock client, and the session bypass."""

import backend.kite_client as kc
import backend.main as m
from backend.dev_mode import is_dev_mode
from backend.mock_kite_client import MockKiteClient


class TestFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("KITE_DEV_MODE", raising=False)
        assert is_dev_mode() is False

    def test_truthy_values(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("KITE_DEV_MODE", val)
            assert is_dev_mode() is True

    def test_falsy_values(self, monkeypatch):
        for val in ("0", "false", "no", "", "off"):
            monkeypatch.setenv("KITE_DEV_MODE", val)
            assert is_dev_mode() is False


class TestClientSelection:
    def test_make_client_returns_mock_in_dev(self, monkeypatch):
        monkeypatch.setenv("KITE_DEV_MODE", "1")
        assert isinstance(kc._make_client(), MockKiteClient)

    def test_make_client_returns_real_when_off(self, monkeypatch):
        monkeypatch.delenv("KITE_DEV_MODE", raising=False)
        assert isinstance(kc._make_client(), kc.KiteClient)


class TestMockMarketData:
    def setup_method(self):
        self.mock = MockKiteClient()

    def test_instruments_non_empty_and_shaped(self):
        insts = self.mock.get_instruments("NSE")
        assert len(insts) >= 5
        i = insts[0]
        assert {"tradingsymbol", "instrument_token", "tick_size"} <= i.keys()

    def test_ltp_returns_price_per_key(self):
        out = self.mock.get_ltp(["NSE:RELIANCE", "NSE:INFY"])
        assert out["NSE:RELIANCE"]["last_price"] > 0
        assert out["NSE:INFY"]["last_price"] > 0

    def test_quote_has_ohlc_and_volume(self):
        q = self.mock.get_quote(["NSE:RELIANCE"])["NSE:RELIANCE"]
        assert q["last_price"] > 0
        assert {"open", "high", "low", "close"} <= q["ohlc"].keys()
        assert q["volume"] > 0

    def test_historical_returns_enough_numeric_candles(self):
        candles = self.mock.get_historical_data(738561, None, None, "5minute")
        assert len(candles) >= 35  # above every strategy's minimum
        c = candles[0]
        for k in ("open", "high", "low", "close", "volume"):
            assert isinstance(c[k], (int, float))
        # High/low bracket the open/close.
        assert c["high"] >= max(c["open"], c["close"])
        assert c["low"] <= min(c["open"], c["close"])

    def test_historical_is_deterministic(self):
        a = self.mock.get_historical_data(738561, None, None, "5minute")
        b = self.mock.get_historical_data(738561, None, None, "5minute")
        assert a[0]["close"] == b[0]["close"]

    def test_margins_shape(self):
        bal = self.mock.get_margins()["equity"]["available"]["live_balance"]
        assert bal > 0

    def test_empty_account_book(self):
        assert self.mock.get_positions() == {"net": [], "day": []}
        assert self.mock.get_orders() == []
        assert self.mock.get_holdings() == []

    def test_search_filters(self):
        res = self.mock.search_instruments("REL")
        assert all("REL" in i["tradingsymbol"] for i in res)


class TestMockOrders:
    def setup_method(self):
        self.mock = MockKiteClient()

    def test_place_order_returns_id_and_sends_nothing(self):
        oid = self.mock.place_order(
            variety="regular",
            exchange="NSE",
            tradingsymbol="RELIANCE",
            transaction_type="BUY",
            quantity=10,
            product="MIS",
            order_type="MARKET",
        )
        assert isinstance(oid, str) and oid
        assert self.mock.get_positions()["net"] == []  # nothing actually traded

    def test_cancel_and_modify_are_safe_noops(self):
        assert self.mock.cancel_order("regular", "X1")["order_id"] == "X1"
        assert self.mock.modify_order(variety="regular", order_id="X1") == {
            "order_id": "X1"
        }

    def test_generate_session_returns_dev_token(self):
        s = self.mock.generate_session("req", "secret")
        assert s["access_token"] == "dev-token"


class TestSessionBypass:
    def test_check_session_valid_without_credentials_in_dev(self, monkeypatch):
        monkeypatch.setattr(m, "is_dev_mode", lambda: True)
        res = m.handle_request({"method": "check_session", "id": 1})
        assert res["result"] == {"is_valid": True}

    def test_check_session_invalid_without_credentials_when_off(self, monkeypatch):
        monkeypatch.setattr(m, "is_dev_mode", lambda: False)
        monkeypatch.setattr(m.config_manager, "get_credentials", lambda: {})
        res = m.handle_request({"method": "check_session", "id": 1})
        assert res["result"] == {"is_valid": False}


class TestCoversScanUniverse:
    """Regression: the mock must cover the full NIFTY 100 scan universe, or the
    screener/scanner produce nothing and every data screen is empty."""

    def setup_method(self):
        self.mock = MockKiteClient()

    def test_instruments_cover_full_nifty100(self):
        from backend.nifty_universe import NIFTY_100

        symbols = {i["tradingsymbol"] for i in self.mock.get_instruments("NSE")}
        assert set(NIFTY_100) <= symbols

    def test_quote_works_for_arbitrary_universe_symbol(self):
        # A symbol beyond the old 8-symbol table used to crash (token 0 ->
        # empty candles -> IndexError). Now every symbol resolves.
        q = self.mock.get_quote(["NSE:ADANIENT", "NSE:WIPRO"])
        assert q["NSE:ADANIENT"]["last_price"] > 0
        assert {"open", "high", "low", "close"} <= q["NSE:ADANIENT"]["ohlc"].keys()

    def test_candles_non_empty_for_any_universe_token(self):
        from backend.mock_kite_client import _token_for

        for sym in ("ADANIENT", "WIPRO", "HINDALCO"):
            candles = self.mock.get_historical_data(
                _token_for(sym), None, None, "5minute"
            )
            assert len(candles) >= 35

    def test_screener_and_scan_produce_signals_end_to_end(self, monkeypatch):
        # The exact user-visible failure: scan_now returned [] because the
        # screener fell back to real NIFTY 100 names the mock didn't know.
        import backend.kite_client as kc
        import backend.scanner as sc
        import backend.screener as scr

        monkeypatch.setattr(kc, "kite_client", self.mock)
        monkeypatch.setattr(sc, "kite_client", self.mock)
        monkeypatch.setattr(scr, "kite_client", self.mock)

        from backend.nifty_universe import NIFTY_100

        watchlist = scr.screener_engine.generate_daily_watchlist(
            universe=NIFTY_100, limit=12
        )
        assert len(watchlist) == 12
        assert set(watchlist) <= set(NIFTY_100)

        signals = sc.scanner.scan_watchlist(watchlist)
        assert len(signals) > 0  # real signals off synthetic candles
