import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List


class TradeJournal:
    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path.home() / ".kite-agentic-trading" / "journal.db"
        else:
            self.db_path = Path(db_path)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        """Get thread-local SQLite connection."""
        if not hasattr(self._local, "conn"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit mode
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        with conn:
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

                CREATE TABLE IF NOT EXISTS trade_events (
                    id TEXT PRIMARY KEY,
                    trade_id TEXT,
                    timestamp TIMESTAMP,
                    event_type TEXT,
                    details TEXT,
                    FOREIGN KEY(trade_id) REFERENCES trades(id)
                );
            """)

    def open_trade(
        self,
        trade_id: str,
        tradingsymbol: str,
        exchange: str,
        direction: str,
        product: str,
        strategy: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        target: float,
        signal_id: Optional[str] = None,
        reasoning: Optional[str] = None,
        confidence: Optional[int] = None,
        confluence_snapshot: Optional[Dict[str, Any]] = None,
        indicator_snapshot: Optional[Dict[str, Any]] = None,
    ):
        """Record a newly opened trade."""
        conn = self._get_conn()
        now = datetime.now().isoformat()

        confluence_str = (
            json.dumps(confluence_snapshot) if confluence_snapshot else None
        )
        indicator_str = json.dumps(indicator_snapshot) if indicator_snapshot else None

        query = """
            INSERT INTO trades (
                id, tradingsymbol, exchange, direction, product, strategy,
                signal_id, reasoning, confidence, entry_price, quantity,
                stop_loss, target, entry_time, status, confluence_snapshot, indicator_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """
        params = (
            trade_id,
            tradingsymbol,
            exchange,
            direction,
            product,
            strategy,
            signal_id,
            reasoning,
            confidence,
            entry_price,
            quantity,
            stop_loss,
            target,
            now,
            confluence_str,
            indicator_str,
        )

        with conn:
            conn.execute(query, params)
            self._log_event_inner(
                conn,
                trade_id,
                now,
                "entry_filled",
                {"entry_price": entry_price, "quantity": quantity},
            )

    def log_event(self, trade_id: str, event_type: str, details: Dict[str, Any]):
        """Append an event to a trade's timeline."""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        with conn:
            self._log_event_inner(conn, trade_id, now, event_type, details)

    def _log_event_inner(
        self,
        conn,
        trade_id: str,
        timestamp: str,
        event_type: str,
        details: Dict[str, Any],
    ):
        event_id = str(uuid.uuid4())
        details_str = json.dumps(details)
        query = """
            INSERT INTO trade_events (id, trade_id, timestamp, event_type, details)
            VALUES (?, ?, ?, ?, ?)
        """
        conn.execute(query, (event_id, trade_id, timestamp, event_type, details_str))

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str):
        """Mark a trade as closed and record its outcome."""
        conn = self._get_conn()
        now = datetime.now().isoformat()

        # Calculate PNL
        cursor = conn.execute(
            "SELECT direction, entry_price, quantity FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = cursor.fetchone()

        pnl = 0.0
        if row:
            direction, entry_price, quantity = row
            if direction == "BUY":
                pnl = (exit_price - entry_price) * quantity
            else:
                pnl = (entry_price - exit_price) * quantity

        query = """
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?
            WHERE id = ?
        """
        with conn:
            conn.execute(query, (exit_price, now, exit_reason, pnl, trade_id))
            self._log_event_inner(
                conn,
                trade_id,
                now,
                "exit_filled",
                {"exit_price": exit_price, "exit_reason": exit_reason, "pnl": pnl},
            )

    def get_trades(self) -> List[Dict[str, Any]]:
        """Get all trades ordered by entry time descending."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM trades ORDER BY entry_time DESC")
        return [dict(row) for row in cursor.fetchall()]

    def get_trade_events(self, trade_id: str) -> List[Dict[str, Any]]:
        """Get timeline events for a specific trade, ordered by timestamp."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM trade_events WHERE trade_id = ? ORDER BY timestamp ASC",
            (trade_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


journal = TradeJournal()
