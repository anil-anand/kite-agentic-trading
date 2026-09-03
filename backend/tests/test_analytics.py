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
        VALUES ('1', 'TCS', 'MACD', 'BUY', 85, 100, 95, 10, 110, 100, ?, ?, 'target_hit', 'CLOSED', '{"MACD": {}}')
    """,
        (t1_entry.isoformat(), t1_exit.isoformat()),
    )

    conn.execute(
        """
        INSERT INTO trades (id, tradingsymbol, strategy, direction, confidence, entry_price, stop_loss, quantity, exit_price, pnl, entry_time, exit_time, exit_reason, status, confluence_snapshot)
        VALUES ('2', 'INFY', 'MACD', 'SELL', 75, 100, 105, 10, 110, -100, ?, ?, 'stop_hit', 'CLOSED', '{"MACD": {}, "RSI": {}}')
    """,
        (t1_entry.isoformat(), t1_exit.isoformat()),
    )

    conn.commit()
    conn.close()

    return str(db_file)


def test_strategy_expectancy(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_strategy_expectancy()
    assert len(res) == 1
    macd = res[0]
    assert macd["strategy"] == "MACD"
    assert macd["total_trades"] == 2
    assert macd["win_rate_pct"] == 50.0
    assert macd["profit_factor"] == 1.0


def test_confluence_validation(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_confluence_validation()
    assert len(res) == 2
    # one trade has 1 strategy, another has 2
    res_1 = next(r for r in res if r["confluence_count"] == 1)
    assert res_1["total_trades"] == 1
    assert res_1["total_pnl"] == 100

    res_2 = next(r for r in res if r["confluence_count"] == 2)
    assert res_2["total_trades"] == 1
    assert res_2["total_pnl"] == -100


def test_confidence_calibration(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_confidence_calibration()
    assert len(res) == 2
    # 70-79 bucket (75 conf) -> 1 trade, 0 wins
    # 80-89 bucket (85 conf) -> 1 trade, 1 wins
    b70 = next(r for r in res if r["confidence_bucket"] == "70-79")
    assert b70["total_trades"] == 1
    assert b70["actual_win_rate_pct"] == 0.0

    b80 = next(r for r in res if r["confidence_bucket"] == "80-89")
    assert b80["total_trades"] == 1
    assert b80["actual_win_rate_pct"] == 100.0


def test_exit_reason_effectiveness(temp_db):
    analytics = TradeAnalytics(db_path=temp_db)
    res = analytics.get_exit_reason_effectiveness()
    assert len(res) == 2
    # target_hit, stop_hit
    target = next(r for r in res if r["exit_reason"] == "target_hit")
    assert target["total_pnl"] == 100
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
                "date": now - timedelta(minutes=5),
                "open": 100,
                "high": 115,
                "low": 98,
                "close": 110,
            },
            {"date": now, "open": 110, "high": 112, "low": 105, "close": 108},
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
    mock_generate.assert_called_once()
