import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.trading_costs import TradingCostCalculator


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
                    estimated_probability REAL,
                    calibration_sample_size INTEGER,
                    entry_price REAL,
                    quantity INTEGER,
                    stop_loss REAL,
                    target REAL,
                    entry_time TIMESTAMP,
                    exit_price REAL,
                    exit_time TIMESTAMP,
                    exit_reason TEXT,
                    pnl REAL,
                    gross_pnl REAL,
                    net_pnl REAL,
                    brokerage REAL,
                    taxes REAL,
                    exchange_charges REAL,
                    other_fees REAL,
                    slippage REAL,
                    signal_entry_price REAL,
                    status TEXT,
                    confluence_snapshot TEXT,
                    indicator_snapshot TEXT,
                    universe_version TEXT,
                    screener_ranking INTEGER,
                    market_regime TEXT,
                    strategy_family TEXT,
                    production_playbook TEXT,
                    raw_evidence TEXT,
                    feature_values TEXT,
                    signal_time TIMESTAMP,
                    candle_time TIMESTAMP,
                    entry_quote REAL,
                    exit_quote REAL,
                    stop_distance REAL,
                    target_distance REAL,
                    initial_r REAL,
                    realized_r REAL,
                    mae REAL,
                    mfe REAL,
                    holding_time_seconds INTEGER,
                    screener_score REAL,
                    strategy_version TEXT
                );

                CREATE TABLE IF NOT EXISTS trade_events (
                    id TEXT PRIMARY KEY,
                    trade_id TEXT,
                    timestamp TIMESTAMP,
                    event_type TEXT,
                    details TEXT,
                    FOREIGN KEY(trade_id) REFERENCES trades(id)
                );

                CREATE TABLE IF NOT EXISTS llm_post_mortems (
                    trade_id TEXT PRIMARY KEY,
                    cache_key TEXT,
                    provider TEXT,
                    model TEXT,
                    prompt_version TEXT,
                    analysis TEXT,
                    created_at TIMESTAMP,
                    FOREIGN KEY(trade_id) REFERENCES trades(id)
                );
            """)

            # Backwards compatibility for existing DBs
            try:
                conn.execute(
                    "ALTER TABLE trades ADD COLUMN estimated_probability REAL;"
                )
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute(
                    "ALTER TABLE trades ADD COLUMN calibration_sample_size INTEGER;"
                )
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE trades ADD COLUMN universe_version TEXT;")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE trades ADD COLUMN screener_ranking INTEGER;")
            except sqlite3.OperationalError:
                pass

            new_columns = [
                "gross_pnl REAL",
                "net_pnl REAL",
                "brokerage REAL",
                "taxes REAL",
                "exchange_charges REAL",
                "other_fees REAL",
                "slippage REAL",
                "signal_entry_price REAL",
                "market_regime TEXT",
                "strategy_family TEXT",
                "production_playbook TEXT",
                "raw_evidence TEXT",
                "feature_values TEXT",
                "signal_time TIMESTAMP",
                "candle_time TIMESTAMP",
                "entry_quote REAL",
                "exit_quote REAL",
                "stop_distance REAL",
                "target_distance REAL",
                "initial_r REAL",
                "realized_r REAL",
                "mae REAL",
                "mfe REAL",
                "holding_time_seconds INTEGER",
                "screener_score REAL",
                "strategy_version TEXT",
            ]
            for col in new_columns:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col};")
                except sqlite3.OperationalError:
                    pass

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
        signal_score: Optional[int] = None,
        estimated_probability: Optional[float] = None,
        calibration_sample_size: Optional[int] = None,
        confluence_snapshot: Optional[Dict[str, Any]] = None,
        indicator_snapshot: Optional[Dict[str, Any]] = None,
        universe_version: Optional[str] = None,
        screener_ranking: Optional[int] = None,
        signal_entry_price: Optional[float] = None,
        market_regime: Optional[str] = None,
        strategy_family: Optional[str] = None,
        production_playbook: Optional[str] = None,
        raw_evidence: Optional[str] = None,
        feature_values: Optional[str] = None,
        signal_time: Optional[str] = None,
        candle_time: Optional[str] = None,
        entry_quote: Optional[float] = None,
        screener_score: Optional[float] = None,
        strategy_version: Optional[str] = None,
        stop_distance: Optional[float] = None,
        target_distance: Optional[float] = None,
        initial_r: Optional[float] = None,
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
                signal_id, reasoning, confidence, estimated_probability, calibration_sample_size,
                entry_price, quantity, stop_loss, target, entry_time, status, confluence_snapshot, indicator_snapshot,
                universe_version, screener_ranking, signal_entry_price,
                gross_pnl, net_pnl, brokerage, taxes, exchange_charges, other_fees, slippage,
                market_regime, strategy_family, production_playbook, raw_evidence, feature_values,
                signal_time, candle_time, entry_quote, screener_score, strategy_version,
                stop_distance, target_distance, initial_r
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            signal_score,
            estimated_probability,
            calibration_sample_size,
            entry_price,
            quantity,
            stop_loss,
            target,
            now,
            confluence_str,
            indicator_str,
            universe_version,
            screener_ranking,
            signal_entry_price,
            market_regime,
            strategy_family,
            production_playbook,
            raw_evidence,
            feature_values,
            signal_time,
            candle_time,
            entry_quote,
            screener_score,
            strategy_version,
            stop_distance,
            target_distance,
            initial_r,
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

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        exit_time: Optional[str] = None,
        cost_details: Optional[Dict[str, Any]] = None,
        exit_quote: Optional[float] = None,
        realized_r: Optional[float] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        holding_time_seconds: Optional[int] = None,
    ):
        """Mark a trade as closed and record its outcome."""
        conn = self._get_conn()
        now = exit_time or datetime.now().isoformat()

        # Calculate PNL
        cursor = conn.execute(
            "SELECT direction, entry_price, quantity FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = cursor.fetchone()

        pnl = 0.0
        gross_pnl = 0.0
        net_pnl = 0.0
        brokerage = 0.0
        taxes = 0.0
        exchange_charges = 0.0
        other_fees = 0.0
        slippage = 0.0

        if row:
            direction, entry_price, quantity = row
            if cost_details:
                gross_pnl = cost_details.get("gross_pnl", 0.0)
                net_pnl = cost_details.get("net_pnl", 0.0)
                brokerage = cost_details.get("brokerage", 0.0)
                taxes = cost_details.get("taxes", 0.0)
                exchange_charges = cost_details.get("exchange_charges", 0.0)
                other_fees = cost_details.get("other_fees", 0.0)
                slippage = cost_details.get("slippage", 0.0)
                pnl = net_pnl
            else:
                calc = TradingCostCalculator()
                charges = calc.calculate_trade_charges(
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                )
                gross_pnl = charges["gross_pnl"]
                net_pnl = charges["net_pnl"]
                brokerage = charges["brokerage"]
                taxes = charges["taxes"]
                exchange_charges = charges["exchange_charges"]
                other_fees = charges["other_fees"]
                slippage = charges["slippage"]
                pnl = net_pnl

        query = """
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?,
                gross_pnl = ?, net_pnl = ?, brokerage = ?, taxes = ?, exchange_charges = ?,
                other_fees = ?, slippage = ?,
                exit_quote = ?, realized_r = ?, mae = ?, mfe = ?, holding_time_seconds = ?
            WHERE id = ?
        """
        with conn:
            conn.execute(
                query,
                (
                    exit_price,
                    now,
                    exit_reason,
                    pnl,
                    gross_pnl,
                    net_pnl,
                    brokerage,
                    taxes,
                    exchange_charges,
                    other_fees,
                    slippage,
                    exit_quote,
                    realized_r,
                    mae,
                    mfe,
                    holding_time_seconds,
                    trade_id,
                ),
            )
            self._log_event_inner(
                conn,
                trade_id,
                now,
                "exit_filled",
                {"exit_price": exit_price, "exit_reason": exit_reason, "pnl": pnl},
            )

    def update_trade_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        exit_time: str,
        cost_details: Optional[Dict[str, Any]] = None,
        exit_quote: Optional[float] = None,
        realized_r: Optional[float] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        holding_time_seconds: Optional[int] = None,
    ):
        """Update an already closed or unreconciled trade with actual execution details."""
        conn = self._get_conn()

        cursor = conn.execute(
            "SELECT direction, entry_price, quantity FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = cursor.fetchone()

        pnl = 0.0
        gross_pnl = 0.0
        net_pnl = 0.0
        brokerage = 0.0
        taxes = 0.0
        exchange_charges = 0.0
        other_fees = 0.0
        slippage = 0.0

        if row:
            direction, entry_price, quantity = row
            if cost_details:
                gross_pnl = cost_details.get("gross_pnl", 0.0)
                net_pnl = cost_details.get("net_pnl", 0.0)
                brokerage = cost_details.get("brokerage", 0.0)
                taxes = cost_details.get("taxes", 0.0)
                exchange_charges = cost_details.get("exchange_charges", 0.0)
                other_fees = cost_details.get("other_fees", 0.0)
                slippage = cost_details.get("slippage", 0.0)
                pnl = net_pnl
            else:
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * quantity
                else:
                    pnl = (entry_price - exit_price) * quantity
                gross_pnl = pnl
                net_pnl = pnl

        query = """
            UPDATE trades
            SET exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?,
                gross_pnl = ?, net_pnl = ?, brokerage = ?, taxes = ?, exchange_charges = ?,
                other_fees = ?, slippage = ?,
                exit_quote = ?, realized_r = ?, mae = ?, mfe = ?, holding_time_seconds = ?
            WHERE id = ?
        """
        with conn:
            conn.execute(
                query,
                (
                    exit_price,
                    exit_time,
                    exit_reason,
                    pnl,
                    gross_pnl,
                    net_pnl,
                    brokerage,
                    taxes,
                    exchange_charges,
                    other_fees,
                    slippage,
                    exit_quote,
                    realized_r,
                    mae,
                    mfe,
                    holding_time_seconds,
                    trade_id,
                ),
            )
            self._log_event_inner(
                conn,
                trade_id,
                datetime.now().isoformat(),
                "exit_reconciled",
                {
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "actual_exit_time": exit_time,
                },
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

    def get_todays_trade_counts(self) -> Dict[str, Any]:
        """Returns the total number of trades opened today, and trades per symbol."""
        conn = self._get_conn()
        today = datetime.now().date().isoformat()

        cursor = conn.execute(
            """
            SELECT tradingsymbol FROM trades
            WHERE entry_time >= ?
        """,
            (today,),
        )

        counts = {"total": 0, "by_symbol": {}}
        for row in cursor.fetchall():
            symbol = row[0]
            counts["total"] += 1
            counts["by_symbol"][symbol] = counts["by_symbol"].get(symbol, 0) + 1

        return counts

    def get_last_exit_time(self, symbol: str) -> Optional[datetime]:
        """Returns the last time a trade was exited for a given symbol."""
        conn = self._get_conn()
        cursor = conn.execute(
            """
            SELECT exit_time FROM trades
            WHERE tradingsymbol = ? AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 1
        """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except ValueError:
                pass
        return None


journal = TradeJournal()
