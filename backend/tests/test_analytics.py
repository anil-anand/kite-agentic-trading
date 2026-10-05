import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.analytics import TradeAnalytics


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "journal.db"

    # Initialize the tables
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            tradingsymbol TEXT,
            exchange TEXT,
            direction TEXT,
            product TEXT,
            strategy TEXT,
            signal_id TEXT,
            reasoning TEXT,
            confidence INTEGER,
            entry_price REAL,
            quantity INTEGER,
            stop_loss REAL,
            target REAL,
            entry_time TIMESTAMP,
            exit_price REAL,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            pnl REAL,
            status TEXT,
            confluence_snapshot TEXT,
            indicator_snapshot TEXT
        );
    """)

    # Insert some dummy closed trades
    now = datetime.now()
    t1_entry = now - timedelta(minutes=10)
    t1_exit = now

    conn.execute(
        """
        INSERT INTO trades (id, tradingsymbol, strategy, direction, confidence, entry_price, stop_loss, quantity, exit_price, pnl, entry_time, exit_time, exit_reason, status, confluence_snapshot)
        VALUES ('1', 'TCS', 'MACD', 'BUY', 85, 100, 95, 10, 110, 100, ?, ?, 'target_hit', 'CLOSED', '{"strategies": [{"strategy": "MACD", "direction": "BUY"}]}')
    """,
        (t1_entry.isoformat(), t1_exit.isoformat()),
    )

    conn.execute(
        """
        INSERT INTO trades (id, tradingsymbol, strategy, direction, confidence, entry_price, stop_loss, quantity, exit_price, pnl, entry_time, exit_time, exit_reason, status, confluence_snapshot)
        VALUES ('2', 'INFY', 'MACD', 'SELL', 75, 100, 105, 10, 110, -100, ?, ?, 'stop_hit', 'CLOSED', '{"strategies": [{"strategy": "MACD", "direction": "SELL"}, {"strategy": "RSI", "direction": "SELL"}]}')
    """,
        (t1_entry.isoformat(), t1_exit.isoformat()),
    )

    conn.execute(
        """
        INSERT INTO trades (id, tradingsymbol, strategy, direction, confidence, entry_price, stop_loss, quantity, exit_price, pnl, entry_time, exit_time, exit_reason, status, confluence_snapshot)
        VALUES ('3', 'WIPRO', 'RSI', 'BUY', 75, 100, 95, 10, 110, 100, ?, ?, 'target_hit', 'CLOSED', '{"regime": "BULL", "buy_signals": 1}')
    """,
        (t1_entry.isoformat(), t1_exit.isoformat()),
    )

    conn.commit()
    conn.close()

    return str(db_file)


def test_strategy_expectancy(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_strategy_expectancy()
    assert len(res) == 2
    macd = next(r for r in res if r["strategy"] == "MACD")
    assert macd["strategy"] == "MACD"
    assert macd["total_trades"] == 2
    assert macd["win_rate_pct"] == 50.0
    assert macd["profit_factor"] == 1.0


def test_confluence_validation(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_confluence_validation()
    assert len(res) == 3
    # one trade has 1 strategy, another has 2, another is invalid
    res_1 = next(r for r in res if r["confluence_count"] == 1)
    assert res_1["total_trades"] == 1
    assert res_1["total_pnl"] == 100

    res_2 = next(r for r in res if r["confluence_count"] == 2)
    assert res_2["total_trades"] == 1
    assert res_2["total_pnl"] == -100

    res_inv = next(r for r in res if r["confluence_count"] == "invalid")
    assert res_inv["total_trades"] == 1
    assert res_inv["total_pnl"] == 100


def test_signal_score_calibration(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_signal_score_calibration()
    assert len(res) == 2
    # 70-79 bucket (75 conf) -> 2 trades, 1 win, 1 loss
    # 80-89 bucket (85 conf) -> 1 trade, 1 wins
    b70 = next(r for r in res if r["signal_score_bucket"] == "70-79")
    assert b70["total_trades"] == 2
    assert b70["actual_win_rate_pct"] == 50.0

    b80 = next(r for r in res if r["signal_score_bucket"] == "80-89")
    assert b80["total_trades"] == 1
    assert b80["actual_win_rate_pct"] == 100.0


def test_exit_reason_effectiveness(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_exit_reason_effectiveness()
    assert len(res) == 2
    # target_hit, stop_hit
    target = next(r for r in res if r["exit_reason"] == "target_hit")
    assert target["total_trades"] == 2
    assert target["total_pnl"] == 200.0

    stop = next(r for r in res if r["exit_reason"] == "stop_hit")
    assert stop["total_pnl"] == -100


@patch("backend.kite_client.kite_client.get_historical_data")
@patch("backend.kite_client.kite_client.get_instruments")
def test_trade_replay(mock_instruments, mock_historical, temp_db):
    mock_instruments.return_value = [
        {"tradingsymbol": "TCS", "instrument_token": 12345}
    ]
    mock_historical.return_value = [
        {"date": datetime.now(), "open": 100, "high": 105, "low": 95, "close": 102}
    ]

    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_trade_replay("1")

    assert "error" not in res
    assert res["trade"]["tradingsymbol"] == "TCS"
    assert len(res["candles"]) == 1


@patch("backend.analytics.TradeAnalytics.get_trade_replay")
def test_what_if_analysis(mock_replay, temp_db):
    now = datetime.now()
    mock_replay.return_value = {
        "trade": {
            "tradingsymbol": "TCS",
            "entry_price": 100,
            "direction": "BUY",
            "quantity": 10,
            "target": 110,
            "stop_loss": 95,
            "entry_time": (now - timedelta(minutes=10)).isoformat(),
            "pnl": 100,
        },
        "candles": [
            {
                "time": int((now - timedelta(minutes=5)).timestamp()),
                "open": 100,
                "high": 115,
                "low": 98,
                "close": 110,
            },
            {
                "time": int(now.timestamp()),
                "open": 110,
                "high": 112,
                "low": 105,
                "close": 108,
            },
        ],
    }

    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_what_if_analysis("1")

    assert "error" not in res
    assert res["target_hit"] is True
    assert res["eod_pnl"] == (108 - 100) * 10
    assert res["wider_stop_hit"] is False


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "This is a post-mortem analysis."

    analytics = TradeAnalytics(db_path=temp_db)
    # create table trade_events in temp_db for this test to not crash
    conn = sqlite3.connect(temp_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_events (
            id TEXT PRIMARY KEY,
            trade_id TEXT,
            timestamp TIMESTAMP,
            event_type TEXT,
            details TEXT
        );
    """)
    conn.close()

    res = analytics.generate_llm_post_mortem("1")
    assert "error" not in res
    assert res["analysis"] == "This is a post-mortem analysis."
    assert res["cached"] is False
    mock_generate.assert_called_once()


def _prepare_trade_events_table(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_events (
            id TEXT PRIMARY KEY,
            trade_id TEXT,
            timestamp TIMESTAMP,
            event_type TEXT,
            details TEXT
        );
    """)
    conn.close()


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_cache_hit(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """A second call for the same trade/provider/model should not re-call the LLM."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "This is a post-mortem analysis."

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)

    first = analytics.generate_llm_post_mortem("1")
    assert first["analysis"] == "This is a post-mortem analysis."
    assert first["cached"] is False

    second = analytics.generate_llm_post_mortem("1")
    assert second["analysis"] == "This is a post-mortem analysis."
    assert second["cached"] is True

    mock_generate.assert_called_once()


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_cache_survives_new_instance(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """The cache must persist to disk, not just in-process memory."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "This is a post-mortem analysis."

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)
    analytics.generate_llm_post_mortem("1")

    # Simulate a fresh process/renderer reload by using a brand new instance.
    other_analytics = TradeAnalytics(db_path=temp_db)
    res = other_analytics.generate_llm_post_mortem("1")
    assert res["cached"] is True
    mock_generate.assert_called_once()


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_invalidates_on_model_change(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """Changing the configured model should regenerate instead of using the cache."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "First analysis."

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)
    first = analytics.generate_llm_post_mortem("1")
    assert first["analysis"] == "First analysis."
    assert first["cached"] is False

    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-pro",
    }
    mock_generate.return_value = "Second analysis with new model."

    second = analytics.generate_llm_post_mortem("1")
    assert second["analysis"] == "Second analysis with new model."
    assert second["cached"] is False
    assert mock_generate.call_count == 2


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_invalidates_on_trade_change(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """Changed trade inputs (e.g. new timeline events) should regenerate."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "First analysis."

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)
    first = analytics.generate_llm_post_mortem("1")
    assert first["cached"] is False

    # Simulate the trade data changing (e.g. a new event was logged).
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO trade_events (id, trade_id, timestamp, event_type, details) "
        "VALUES ('evt-1', '1', '2024-01-01T00:00:00', 'note', 'late fill info')"
    )
    conn.commit()
    conn.close()

    mock_generate.return_value = "Updated analysis."
    second = analytics.generate_llm_post_mortem("1")
    assert second["analysis"] == "Updated analysis."
    assert second["cached"] is False
    assert mock_generate.call_count == 2


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_failure_not_cached(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """Transient LLM failures must not be persisted, so retries can succeed."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)

    mock_generate.side_effect = RuntimeError("upstream timeout")
    failed = analytics.generate_llm_post_mortem("1")
    assert "error" in failed
    assert "cached" not in failed

    mock_generate.side_effect = None
    mock_generate.return_value = "Recovered analysis."
    recovered = analytics.generate_llm_post_mortem("1")
    assert recovered["analysis"] == "Recovered analysis."
    assert recovered["cached"] is False
    assert mock_generate.call_count == 2


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_returns_analysis_when_cache_write_fails(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """A successful analysis must still be returned even if persisting it fails."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }
    mock_generate.return_value = "This is a post-mortem analysis."

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)
    real_get_conn = analytics._get_conn

    class _FailingInsertConn:
        """Wraps a real sqlite3 connection, failing only INSERTs into the cache table."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO llm_post_mortems" in sql:
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, *exc_info):
            return self._conn.__exit__(*exc_info)

    def _wrapped_get_conn():
        return _FailingInsertConn(real_get_conn())

    with patch.object(analytics, "_get_conn", _wrapped_get_conn):
        res = analytics.generate_llm_post_mortem("1")

    assert "error" not in res
    assert res["analysis"] == "This is a post-mortem analysis."
    assert res["cached"] is False


@patch("backend.config.config_manager.get_credentials")
@patch("backend.config.config_manager.get_llm_settings")
@patch("backend.analytics.OpenAICompatibleClient.generate")
def test_llm_post_mortem_empty_response_not_cached(
    mock_generate, mock_get_llm_settings, mock_get_credentials, temp_db
):
    """An empty/whitespace-only LLM response must not be cached, and should retry."""
    mock_get_credentials.return_value = {"llmApiKey": "fake_key"}
    mock_get_llm_settings.return_value = {
        "provider": "Gemini",
        "baseUrl": "https://example.test/v1",
        "model": "gemini-2.5-flash",
    }

    _prepare_trade_events_table(temp_db)
    analytics = TradeAnalytics(db_path=temp_db)

    mock_generate.return_value = "   "
    empty = analytics.generate_llm_post_mortem("1")
    assert "error" in empty
    assert "cached" not in empty

    mock_generate.return_value = "A real analysis."
    recovered = analytics.generate_llm_post_mortem("1")
    assert recovered["analysis"] == "A real analysis."
    assert recovered["cached"] is False
    assert mock_generate.call_count == 2
