import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.accounting import (
    ACCOUNTING_POLICY_VERSION,
    SUPPORTED_EXCHANGE,
    SUPPORTED_PRODUCT,
    accounting_service,
)
from backend.financial_eligibility import verified_outcome_sql
from backend.time_utils import EXCHANGE_TIMEZONE, as_utc, now_utc


def _entry_session_time(entry_time, observed_at):
    """Use observation time only for session accounting until execution is known."""

    for value in (entry_time, observed_at):
        try:
            timestamp = as_utc(value)
        except (TypeError, ValueError):
            continue
        if timestamp is not None:
            return timestamp
    return None


def _validate_cost_details(cost_details: Dict[str, Any]) -> None:
    """Reject malformed charges without rejecting a legitimately negative P&L."""

    for field in (
        "gross_pnl",
        "net_pnl",
        "slippage",
        "brokerage",
        "taxes",
        "exchange_charges",
        "other_fees",
    ):
        value = cost_details.get(field, 0.0)
        if value is None or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be numeric")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field} must be finite")
        if (
            field in {"brokerage", "taxes", "exchange_charges", "other_fees"}
            and value < 0
        ):
            raise ValueError(f"{field} cannot be negative")


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

    @contextmanager
    def _transaction(self, conn):
        """Run a group of journal writes as one real SQLite transaction.

        Connections deliberately use autocommit so read-only callers never
        retain a transaction.  ``with conn`` is not sufficient in that mode:
        each statement can otherwise survive independently.  Lifecycle rows
        and their event/audit record must either both exist or neither does.
        """

        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
                    strategy_version TEXT,
                    financial_quality TEXT,
                    accounting_policy_version TEXT,
                    cost_model_version TEXT,
                    rounding_version TEXT,
                    financial_provenance TEXT
                    ,namespace TEXT
                    ,account_id TEXT
                    ,instrument_id TEXT
                    ,entry_order_id TEXT
                    ,stop_order_id TEXT
                    ,exit_order_id TEXT
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
                "financial_quality TEXT",
                "accounting_policy_version TEXT",
                "cost_model_version TEXT",
                "rounding_version TEXT",
                "financial_provenance TEXT",
                "namespace TEXT",
                "account_id TEXT",
                "instrument_id TEXT",
                "entry_order_id TEXT",
                "stop_order_id TEXT",
                "execution_linkage_history TEXT",
                "exit_order_id TEXT",
                "entry_observed_at TIMESTAMP",
            ]
            for col in new_columns:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col};")
                except sqlite3.OperationalError:
                    pass

            conn.execute(
                """
                UPDATE trades
                SET status = 'RECONCILIATION_PENDING',
                    financial_quality = 'UNAVAILABLE',
                    financial_provenance = 'legacy_zero_price_placeholder'
                WHERE status = 'CLOSED'
                  AND (exit_price IS NULL OR exit_price <= 0
                       OR exit_reason = 'UNRECONCILED')
                """
            )

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
        namespace: Optional[str] = None,
        account_id: Optional[str] = None,
        instrument_id: Optional[str] = None,
        entry_order_id: Optional[str] = None,
        stop_order_id: Optional[str] = None,
        exit_order_id: Optional[str] = None,
        entry_time: Optional[str] = None,
    ):
        """Record a newly opened trade."""
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if (
            isinstance(entry_price, bool)
            or not isinstance(entry_price, (int, float))
            or not math.isfinite(entry_price)
            or entry_price <= 0
        ):
            raise ValueError("entry_price must be positive")
        conn = self._get_conn()
        executed_at = as_utc(entry_time)
        execution_time = executed_at.isoformat() if executed_at else None
        observed_at = now_utc().isoformat()

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
                stop_distance, target_distance, initial_r,
                namespace, account_id, instrument_id,
                entry_order_id, stop_order_id, exit_order_id, entry_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            execution_time,
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
            namespace,
            account_id,
            instrument_id,
            entry_order_id,
            stop_order_id,
            exit_order_id,
            observed_at,
        )

        immutable = {
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "direction": direction,
            "product": product,
            "entry_price": entry_price,
            "quantity": quantity,
        }
        linkage = {
            "namespace": namespace,
            "account_id": account_id,
            "instrument_id": instrument_id,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "exit_order_id": exit_order_id,
        }
        with self._transaction(conn):
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError:
                # A retry after a process/database fault is valid only if it is
                # the same trade.  Repair fields that an older broken write
                # left NULL, but never silently merge two different trades.
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    "SELECT * FROM trades WHERE id = ?", (trade_id,)
                ).fetchone()
                if existing is None:
                    raise
                for field, expected in immutable.items():
                    actual = existing[field]
                    if field == "entry_price":
                        equal = actual is not None and math.isclose(
                            float(actual), float(expected), abs_tol=0.0001
                        )
                    else:
                        equal = actual == expected
                    if not equal:
                        raise ValueError(
                            f"conflicting duplicate trade id {trade_id}: {field}"
                        )
                repairs = {}
                if execution_time is not None and existing["entry_time"] is None:
                    repairs["entry_time"] = execution_time
                for field, expected in linkage.items():
                    actual = existing[field]
                    if expected in (None, ""):
                        continue
                    if actual in (None, ""):
                        repairs[field] = expected
                    elif str(actual) != str(expected):
                        raise ValueError(
                            f"conflicting duplicate trade id {trade_id}: {field}"
                        )
                if repairs:
                    assignments = ", ".join(f"{field} = ?" for field in repairs)
                    conn.execute(
                        f"UPDATE trades SET {assignments} WHERE id = ?",
                        (*repairs.values(), trade_id),
                    )
                    if "entry_time" in repairs:
                        self._log_event_inner(
                            conn,
                            trade_id,
                            observed_at,
                            "entry_time_reconciled",
                            {
                                "entry_order_id": entry_order_id,
                                "previous_entry_time": existing["entry_time"],
                                "execution_time": execution_time,
                            },
                        )
                event = conn.execute(
                    "SELECT 1 FROM trade_events WHERE trade_id = ? AND event_type = 'entry_filled' LIMIT 1",
                    (trade_id,),
                ).fetchone()
                if event:
                    return trade_id
            self._log_event_inner(
                conn,
                trade_id,
                observed_at,
                "entry_filled",
                {
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "namespace": namespace,
                    "account_id": account_id,
                    "instrument_id": instrument_id,
                    "entry_order_id": entry_order_id,
                    "stop_order_id": stop_order_id,
                    "exit_order_id": exit_order_id,
                    "execution_time": execution_time,
                },
            )
        return trade_id

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row is not None else None

    def reconcile_entry_time(
        self,
        trade_id: str,
        *,
        entry_order_id: str,
        entry_time: datetime | str,
    ) -> None:
        """Persist time verified from the complete canonical entry allocation.

        The caller validates fill identity and quantity. The durable order link
        anchors this repair, including correction of legacy bookkeeping times.
        """

        executed_at = as_utc(entry_time)
        if executed_at is None or not entry_order_id:
            raise ValueError("entry execution time and order linkage are required")
        execution_time = executed_at.isoformat()
        conn = self._get_conn()
        with self._transaction(conn):
            row = conn.execute(
                "SELECT entry_order_id, entry_time FROM trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown trade id {trade_id}")
            if row[0] in (None, "") or str(row[0]) != str(entry_order_id):
                raise ValueError("conflicting entry order linkage for timestamp repair")
            try:
                previous_time = as_utc(row[1])
            except (TypeError, ValueError):
                previous_time = None
            if previous_time == executed_at:
                return
            conn.execute(
                "UPDATE trades SET entry_time = ? WHERE id = ?",
                (execution_time, trade_id),
            )
            self._log_event_inner(
                conn,
                trade_id,
                now_utc().isoformat(),
                "entry_time_reconciled",
                {
                    "entry_order_id": entry_order_id,
                    "previous_entry_time": row[1],
                    "execution_time": execution_time,
                },
            )

    def update_execution_linkage(
        self,
        trade_id: str,
        *,
        entry_order_id: Optional[str] = None,
        stop_order_id: Optional[str] = None,
        exit_order_id: Optional[str] = None,
        verified_predecessors: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """Keep entry immutable; replace terminal reducers with audited evidence.

        Evidence is supplied only by the engine's broker reconciliation path.
        A predecessor must identify this account/instrument and reducing side;
        merely supplying a new ID cannot overwrite a current durable linkage.
        """
        updates = {
            field: str(value)
            for field, value in {
                "entry_order_id": entry_order_id,
                "stop_order_id": stop_order_id,
                "exit_order_id": exit_order_id,
            }.items()
            if value not in (None, "")
        }
        if not updates:
            return
        conn = self._get_conn()
        with self._transaction(conn):
            existing = self.get_trade(trade_id)
            if existing is None:
                raise ValueError(f"trade {trade_id} does not exist")
            history = json.loads(existing.get("execution_linkage_history") or "[]")
            changed = {}
            transitions = []
            for field, value in updates.items():
                current = existing[field]
                if str(current) == value:
                    continue
                if current not in (None, ""):
                    proof = (verified_predecessors or {}).get(field, {})
                    expected_side = "SELL" if existing["direction"] == "BUY" else "BUY"
                    identity_fields = {
                        "namespace": "namespace",
                        "account_id": "account_id",
                        "exchange": "exchange",
                        "product": "product",
                        "instrument_id": "instrument_token",
                        "tradingsymbol": "tradingsymbol",
                    }
                    if (
                        field == "entry_order_id"
                        or str(proof.get("order_id")) != str(current)
                        or proof.get("status")
                        not in {
                            "CANCELLED",
                            "REJECTED",
                            "EXPIRED",
                            "REJECTED AMO",
                            "COMPLETE",
                        }
                        or proof.get("transaction_type") != expected_side
                        or any(
                            existing.get(key) in (None, "", "UNKNOWN")
                            or str(existing[key]) != str(proof.get(broker_key))
                            for key, broker_key in identity_fields.items()
                        )
                    ):
                        raise ValueError(
                            f"conflicting execution linkage for {trade_id}: {field}"
                        )
                    transition = {
                        "field": field,
                        "order_id": str(current),
                        "replacement_order_id": value,
                        "predecessor": proof,
                    }
                    history.append(transition)
                    transitions.append(transition)
                changed[field] = value
            if not changed:
                return
            assignments = ", ".join(f"{field} = ?" for field in changed)
            conn.execute(
                f"UPDATE trades SET {assignments}, execution_linkage_history = ? WHERE id = ?",
                (*changed.values(), json.dumps(history), trade_id),
            )
            self._log_event_inner(
                conn,
                trade_id,
                now_utc().isoformat(),
                "execution_linkage_updated",
                {**changed, "replacements": transitions},
            )

    def log_event(self, trade_id: str, event_type: str, details: Dict[str, Any]):
        """Append an event to a trade's timeline."""
        conn = self._get_conn()
        now = now_utc().isoformat()
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
        exit_price: Optional[float],
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
        if exit_price is not None and (
            isinstance(exit_price, bool)
            or not isinstance(exit_price, (int, float))
            or not math.isfinite(exit_price)
        ):
            raise ValueError("exit_price must be finite")
        if exit_price is not None and exit_price < 0:
            raise ValueError("exit_price cannot be negative")
        if exit_price == 0:
            exit_price = None
        conn = self._get_conn()
        executed_at = as_utc(exit_time)
        now = executed_at.isoformat() if executed_at else None

        # Calculate PNL
        cursor = conn.execute(
            "SELECT direction, entry_price, quantity, exchange, product FROM trades WHERE id = ?",
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
        financial_quality = "UNAVAILABLE"
        financial_provenance = "unknown"

        if exit_price is None:
            query = """
                UPDATE trades
                SET status = 'RECONCILIATION_PENDING', exit_price = NULL, exit_time = ?, exit_reason = ?,
                    pnl = NULL, gross_pnl = NULL, net_pnl = NULL, brokerage = NULL, taxes = NULL,
                    exchange_charges = NULL, other_fees = NULL, slippage = NULL,
                    exit_quote = ?, realized_r = ?, mae = ?, mfe = ?, holding_time_seconds = ?,
                    financial_quality = 'UNAVAILABLE',
                    financial_provenance = 'pending_broker_fill_reconciliation'
                WHERE id = ?
            """
            with self._transaction(conn):
                conn.execute(
                    query,
                    (
                        now,
                        exit_reason,
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
                    now_utc().isoformat(),
                    "exit_pending",
                    {"exit_reason": exit_reason, "expected_exit_time": now},
                )
            return

        if row:
            direction, entry_price, quantity, exchange, product = row
            if exchange != SUPPORTED_EXCHANGE or product != SUPPORTED_PRODUCT:
                raise ValueError(
                    "unsupported or malformed financial inputs: only NSE/MIS is supported"
                )
            if cost_details:
                _validate_cost_details(cost_details)
                gross_pnl = cost_details.get("gross_pnl", 0.0)
                net_pnl = cost_details.get("net_pnl", 0.0)
                brokerage = cost_details.get("brokerage", 0.0)
                taxes = cost_details.get("taxes", 0.0)
                exchange_charges = cost_details.get("exchange_charges", 0.0)
                other_fees = cost_details.get("other_fees", 0.0)
                slippage = cost_details.get("slippage", 0.0)
                pnl = net_pnl
                financial_quality = cost_details.get("financial_quality", "RECONCILED")
                financial_provenance = cost_details.get(
                    "financial_provenance", "allocated_broker_fills"
                )
            else:
                charges = accounting_service.calculate_trade(
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                    exchange=exchange or "",
                    product=product or "",
                )
                if charges is None:
                    raise ValueError("unsupported or malformed financial inputs")
                gross_pnl = charges["gross_pnl"]
                net_pnl = charges["net_pnl"]
                brokerage = charges["brokerage"]
                taxes = charges["taxes"]
                exchange_charges = charges["exchange_charges"]
                other_fees = charges["other_fees"]
                slippage = charges["slippage"]
                pnl = net_pnl
                financial_quality = "ESTIMATED"
                financial_provenance = "journal_derived_mis_projection"

        query = """
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?,
                gross_pnl = ?, net_pnl = ?, brokerage = ?, taxes = ?, exchange_charges = ?,
                other_fees = ?, slippage = ?,
                exit_quote = ?, realized_r = ?, mae = ?, mfe = ?, holding_time_seconds = ?,
                financial_quality = ?, accounting_policy_version = ?, cost_model_version = ?,
                rounding_version = ?, financial_provenance = ?
            WHERE id = ?
        """
        with self._transaction(conn):
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
                    financial_quality,
                    cost_details.get(
                        "accounting_policy_version", ACCOUNTING_POLICY_VERSION
                    )
                    if cost_details
                    else ACCOUNTING_POLICY_VERSION,
                    cost_details.get("cost_model_version", "unknown")
                    if cost_details
                    else "unknown",
                    cost_details.get("rounding_version", "unknown")
                    if cost_details
                    else "unknown",
                    financial_provenance,
                    trade_id,
                ),
            )
            self._log_event_inner(
                conn,
                trade_id,
                now_utc().isoformat(),
                "exit_filled",
                {
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                },
            )

    def update_trade_exit(
        self,
        trade_id: str,
        exit_price: Optional[float],
        exit_reason: str,
        exit_time: str,
        cost_details: Optional[Dict[str, Any]] = None,
        exit_quote: Optional[float] = None,
        realized_r: Optional[float] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        holding_time_seconds: Optional[int] = None,
    ):
        """Update an already pending trade with actual execution details."""
        if exit_price is None or exit_price == 0:
            return self.close_trade(
                trade_id,
                None,
                exit_reason,
                exit_time,
                cost_details=cost_details,
                exit_quote=exit_quote,
                realized_r=realized_r,
                mae=mae,
                mfe=mfe,
                holding_time_seconds=holding_time_seconds,
            )
        if (
            isinstance(exit_price, bool)
            or not isinstance(exit_price, (int, float))
            or not math.isfinite(exit_price)
        ):
            raise ValueError("exit_price must be finite")
        if exit_price < 0:
            raise ValueError("exit_price cannot be negative")
        conn = self._get_conn()

        cursor = conn.execute(
            "SELECT direction, entry_price, quantity, exchange, product FROM trades WHERE id = ?",
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
        financial_quality = "UNAVAILABLE"
        financial_provenance = "unknown"

        if row:
            direction, entry_price, quantity, exchange, product = row
            if exchange != SUPPORTED_EXCHANGE or product != SUPPORTED_PRODUCT:
                raise ValueError(
                    "unsupported or malformed financial inputs: only NSE/MIS is supported"
                )
            if cost_details:
                _validate_cost_details(cost_details)
                gross_pnl = cost_details.get("gross_pnl", 0.0)
                net_pnl = cost_details.get("net_pnl", 0.0)
                brokerage = cost_details.get("brokerage", 0.0)
                taxes = cost_details.get("taxes", 0.0)
                exchange_charges = cost_details.get("exchange_charges", 0.0)
                other_fees = cost_details.get("other_fees", 0.0)
                slippage = cost_details.get("slippage", 0.0)
                pnl = net_pnl
                financial_quality = cost_details.get("financial_quality", "RECONCILED")
                financial_provenance = cost_details.get(
                    "financial_provenance", "allocated_broker_fills"
                )
            else:
                charges = accounting_service.calculate_trade(
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                    exchange=exchange or "",
                    product=product or "",
                )
                if charges is None:
                    return self.close_trade(trade_id, None, exit_reason, exit_time)
                gross_pnl = charges["gross_pnl"]
                net_pnl = charges["net_pnl"]
                brokerage = charges["brokerage"]
                taxes = charges["taxes"]
                exchange_charges = charges["exchange_charges"]
                other_fees = charges["other_fees"]
                slippage = charges["slippage"]
                pnl = net_pnl
                financial_quality = "ESTIMATED"
                financial_provenance = "journal_derived_mis_projection"

        query = """
            UPDATE trades
            SET status = 'CLOSED', exit_price = ?, exit_time = ?, exit_reason = ?, pnl = ?,
                gross_pnl = ?, net_pnl = ?, brokerage = ?, taxes = ?, exchange_charges = ?,
                other_fees = ?, slippage = ?,
                exit_quote = ?, realized_r = ?, mae = ?, mfe = ?, holding_time_seconds = ?,
                financial_quality = ?, accounting_policy_version = ?, cost_model_version = ?,
                rounding_version = ?, financial_provenance = ?
            WHERE id = ?
        """
        with self._transaction(conn):
            conn.execute(
                query,
                (
                    exit_price,
                    as_utc(exit_time).isoformat() if exit_time else None,
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
                    financial_quality,
                    cost_details.get(
                        "accounting_policy_version", ACCOUNTING_POLICY_VERSION
                    )
                    if cost_details
                    else ACCOUNTING_POLICY_VERSION,
                    cost_details.get("cost_model_version", "unknown")
                    if cost_details
                    else "unknown",
                    cost_details.get("rounding_version", "unknown")
                    if cost_details
                    else "unknown",
                    financial_provenance,
                    trade_id,
                ),
            )
            self._log_event_inner(
                conn,
                trade_id,
                now_utc().isoformat(),
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

    def get_verified_todays_outcomes(self) -> List[Dict[str, Any]]:
        """Return verified session outcomes without losing unknown-time entries.

        Observation day retains their risk accounting until the execution day
        can be repaired. It never becomes a displayed execution timestamp.
        """

        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        start = (
            now_utc()
            .astimezone(EXCHANGE_TIMEZONE)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        end = start + timedelta(days=1)
        rows = conn.execute(
            "SELECT * FROM trades WHERE " + verified_outcome_sql(columns)
        ).fetchall()
        result = []
        for row in rows:
            entry_time = _entry_session_time(
                row["entry_time"], row["entry_observed_at"]
            )
            if entry_time is not None and start <= entry_time < end:
                result.append(dict(row))
        return result

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
        """Count entries by execution day, or observation day while time is unknown.

        Missing execution time does not release a consumed admission slot. The
        fallback is only for capacity accounting, never an execution timestamp.
        """
        conn = self._get_conn()
        local_today = (
            now_utc()
            .astimezone(EXCHANGE_TIMEZONE)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        # Compare stored UTC ISO strings with UTC boundaries; a local date
        # string would incorrectly exclude trades made after midnight IST but
        # before 00:00 UTC.
        start_utc = local_today.astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=1)

        cursor = conn.execute(
            """
            SELECT tradingsymbol, entry_time, entry_observed_at, entry_order_id
            FROM trades
            WHERE COALESCE(entry_time, entry_observed_at) IS NOT NULL
        """
        )

        counts = {"total": 0, "by_symbol": {}, "entry_order_ids": set()}
        for row in cursor.fetchall():
            symbol = row[0]
            # The query intentionally reads all non-null timestamps because
            # legacy rows may be naive IST strings while new rows are UTC.
            # Normalize both before applying the exchange-day boundary.
            entry_time = _entry_session_time(row[1], row[2])
            if entry_time is None or not (start_utc <= entry_time < end_utc):
                continue
            counts["total"] += 1
            counts["by_symbol"][symbol] = counts["by_symbol"].get(symbol, 0) + 1
            if row[3] not in (None, ""):
                counts["entry_order_ids"].add(str(row[3]))

        return counts

    def get_last_exit_time(self, symbol: str) -> Optional[datetime]:
        """Returns the last time a trade was exited for a given symbol."""
        conn = self._get_conn()
        unresolved = conn.execute(
            """SELECT 1 FROM trades WHERE tradingsymbol = ?
               AND status IN ('CLOSED', 'RECONCILIATION_PENDING')
               AND exit_time IS NULL LIMIT 1""",
            (symbol,),
        ).fetchone()
        if unresolved:
            raise ValueError(f"Exit execution time unresolved for {symbol}")
        cursor = conn.execute(
            """
            SELECT exit_time FROM trades
            WHERE tradingsymbol = ? AND exit_time IS NOT NULL
            """,
            (symbol,),
        )
        normalized = []
        for row in cursor.fetchall():
            try:
                value = as_utc(row[0])
            except (TypeError, ValueError):
                continue
            if value is not None:
                normalized.append(value)
        return max(normalized) if normalized else None


journal = TradeJournal()
