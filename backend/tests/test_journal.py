import json
import sqlite3
import pytest
from datetime import datetime
from backend.journal import TradeJournal

@pytest.fixture
def temp_journal(tmp_path):
    db_file = tmp_path / "journal.db"
    
    # Initialize the journal with a temporary db path
    journal = TradeJournal(db_path=str(db_file))
    return journal

def test_journal_open_close_trade(temp_journal):
    trade_id = "test_123"
    
    # Open the trade
    temp_journal.open_trade(
        trade_id=trade_id,
        tradingsymbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        product="MIS",
        strategy="MACD_Crossover",
        entry_price=2500.50,
        quantity=10,
        stop_loss=2480.00,
        target=2540.00,
        signal_id="sig_456",
        reasoning="MACD crossed above signal line",
        confidence=85,
        confluence_snapshot={"MACD_Crossover": "BUY"},
        indicator_snapshot={"RSI": 60}
    )
    
    # Check if trade exists
    trades = temp_journal.get_trades()
    assert len(trades) == 1
    assert trades[0]["id"] == trade_id
    assert trades[0]["tradingsymbol"] == "RELIANCE"
    assert trades[0]["status"] == "OPEN"
    assert trades[0]["confidence"] == 85
    
    # Log an event
    temp_journal.log_event(trade_id, "trailing_stop_update", {"new_sl": 2510.00})
    
    # Close the trade
    temp_journal.close_trade(trade_id, exit_price=2520.00, exit_reason="target_hit")
    
    # Verify trade updated
    trades_after = temp_journal.get_trades()
    assert len(trades_after) == 1
    assert trades_after[0]["status"] == "CLOSED"
    assert trades_after[0]["exit_price"] == 2520.00
    assert trades_after[0]["pnl"] == (2520.00 - 2500.50) * 10
    
    # Check events
    events = temp_journal.get_trade_events(trade_id)
    assert len(events) == 3
    
    # The first event is the entry
    assert events[0]["event_type"] == "entry_filled"
    assert json.loads(events[0]["details"])["entry_price"] == 2500.50
    
    # The second event is the log_event
    assert events[1]["event_type"] == "trailing_stop_update"
    assert json.loads(events[1]["details"])["new_sl"] == 2510.00
    
    # The third event is the exit
    assert events[2]["event_type"] == "exit_filled"
    assert json.loads(events[2]["details"])["exit_price"] == 2520.00

def test_get_trades_ordering(temp_journal):
    # Insert multiple trades to test ordering
    # We will just insert directly via conn to simulate different entry times
    conn = temp_journal._get_conn()
    with conn:
        conn.execute("INSERT INTO trades (id, entry_time) VALUES ('t1', '2026-09-01T10:00:00')")
        conn.execute("INSERT INTO trades (id, entry_time) VALUES ('t2', '2026-09-02T10:00:00')")
        conn.execute("INSERT INTO trades (id, entry_time) VALUES ('t3', '2026-08-30T10:00:00')")
        
    trades = temp_journal.get_trades()
    assert len(trades) == 3
    # Should be ordered by entry_time DESC
    assert trades[0]["id"] == "t2"
    assert trades[1]["id"] == "t1"
    assert trades[2]["id"] == "t3"
