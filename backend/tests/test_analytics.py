import sqlite3
from datetime import datetime, timedelta

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
