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

                -- ``order_lifecycle_v1`` is deliberately additive.  Trade
                -- rows remain the financial journal; these records are the
                -- durable execution obligation which exists *before* a trade
                -- can have a verified fill or trade id.
                CREATE TABLE IF NOT EXISTS journal_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    position_key TEXT NOT NULL,
                    trade_id TEXT,
                    intent_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    reason TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    latched INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    attempt_tag TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES order_intents(intent_id)
                );

                CREATE INDEX IF NOT EXISTS idx_order_attempts_intent
                    ON order_attempts(intent_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_order_attempts_broker_order
                    ON order_attempts(broker_order_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_reduction_per_position
                    ON order_intents(position_key)
                    WHERE active = 1 AND intent_type IN ('EXIT', 'FLATTEN');

                CREATE TABLE IF NOT EXISTS order_fill_ledger (
                    broker_fill_id TEXT PRIMARY KEY,
                    intent_id TEXT,
                    attempt_id TEXT,
                    broker_order_id TEXT NOT NULL,
                    position_key TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    fill_price REAL,
                    exchange_time TIMESTAMP,
                    recorded_at TIMESTAMP NOT NULL,
                    raw_fill TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(intent_id) REFERENCES order_intents(intent_id),
                    FOREIGN KEY(attempt_id) REFERENCES order_attempts(attempt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_fill_ledger_order
                    ON order_fill_ledger(broker_order_id);

                CREATE TABLE IF NOT EXISTS order_lifecycle_events (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    attempt_id TEXT,
                    timestamp TIMESTAMP NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES order_intents(intent_id),
                    FOREIGN KEY(attempt_id) REFERENCES order_attempts(attempt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_lifecycle_events_attempt_time
                    ON order_lifecycle_events(attempt_id, timestamp);
            """)

            conn.execute(
                """
                INSERT OR IGNORE INTO journal_schema_migrations(version, applied_at)
                VALUES ('order_lifecycle_v1', ?)
                """,
                (now_utc().isoformat(),),
            )

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

        self._migrate_lifecycle_fill_identity(conn)

    @staticmethod
    def _lifecycle_account_identity(position_key: str) -> tuple[str, str]:
        """Extract the broker account scope without discarding the position epoch."""

        parts = str(position_key).split(":", 2)
        if len(parts) != 3 or any(value in {"", "UNKNOWN"} for value in parts[:2]):
            raise ValueError("canonical namespace/account position key is required")
        return parts[0], parts[1]

    def _migrate_lifecycle_fill_identity(self, conn) -> None:
        """Atomically retain v1 fills while scoping broker IDs to their account."""

        with self._transaction(conn):
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(order_fill_ledger)")
            }
            if "namespace" not in columns:
                conn.execute(
                    """
                    CREATE TABLE order_fill_ledger_v2 (
                        namespace TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        broker_fill_id TEXT NOT NULL,
                        intent_id TEXT,
                        attempt_id TEXT,
                        broker_order_id TEXT NOT NULL,
                        position_key TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity INTEGER NOT NULL CHECK(quantity > 0),
                        fill_price REAL,
                        exchange_time TIMESTAMP,
                        recorded_at TIMESTAMP NOT NULL,
                        raw_fill TEXT NOT NULL DEFAULT '{}',
                        PRIMARY KEY(namespace, account_id, broker_fill_id),
                        FOREIGN KEY(intent_id) REFERENCES order_intents(intent_id),
                        FOREIGN KEY(attempt_id) REFERENCES order_attempts(attempt_id)
                    )
                    """
                )
                cursor = conn.execute("SELECT * FROM order_fill_ledger")
                names = [column[0] for column in cursor.description]
                for values in cursor.fetchall():
                    row = dict(zip(names, values))
                    namespace, account_id = self._lifecycle_account_identity(
                        row["position_key"]
                    )
                    conn.execute(
                        """
                        INSERT INTO order_fill_ledger_v2
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (namespace, account_id, *[row[name] for name in names]),
                    )
                conn.execute("DROP TABLE order_fill_ledger")
                conn.execute(
                    "ALTER TABLE order_fill_ledger_v2 RENAME TO order_fill_ledger"
                )
                conn.execute(
                    "CREATE INDEX idx_order_fill_ledger_order "
                    "ON order_fill_ledger(namespace, account_id, broker_order_id)"
                )
            conn.execute(
                "INSERT OR IGNORE INTO journal_schema_migrations(version, applied_at) "
                "VALUES ('order_lifecycle_fill_identity_v2', ?)",
                (now_utc().isoformat(),),
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

    # Order-lifecycle persistence -------------------------------------------------
    #
    # The methods below intentionally use explicit state versions instead of
    # treating an order acknowledgement as a trade close.  They are used by the
    # phase-2 coordinator and are kept separate from ``trade_events`` because
    # an entry intent can exist before there is a verified trade row to attach
    # it to.

    _ACTIVE_ATTEMPT_STATES = {
        "PREPARED",
        "SUBMITTING",
        "ACKNOWLEDGED",
        "WORKING",
        "PARTIALLY_FILLED",
        "CANCEL_PENDING",
        "UNKNOWN",
    }

    def _lifecycle_event_inner(
        self,
        conn,
        intent_id: str,
        event_type: str,
        details: Dict[str, Any],
        attempt_id: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO order_lifecycle_events
                (id, intent_id, attempt_id, timestamp, event_type, details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                intent_id,
                attempt_id,
                now_utc().isoformat(),
                event_type,
                json.dumps(details, default=str, sort_keys=True),
            ),
        )

    @staticmethod
    def _lifecycle_row(row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        for field in ("payload",):
            try:
                result[field] = json.loads(result[field] or "{}")
            except (TypeError, json.JSONDecodeError):
                result[field] = {}
        result["latched"] = bool(result.get("latched"))
        result["active"] = bool(result.get("active"))
        return result

    def create_order_intent(
        self,
        *,
        intent_id: str,
        position_key: str,
        intent_type: str,
        role: str,
        side: str,
        quantity: int,
        trade_id: Optional[str] = None,
        reason: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        latched: bool = False,
    ) -> Dict[str, Any]:
        """Durably create an obligation before its first broker attempt.

        Repeating the exact call is an idempotent crash-recovery operation;
        changing any immutable identity/quantity field is rejected rather than
        silently merging two broker obligations.
        """

        if not intent_id or not position_key:
            raise ValueError("intent_id and position_key are required")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("intent quantity must be a positive integer")
        intent_type = str(intent_type).upper()
        role = str(role).upper()
        side = str(side).upper()
        if intent_type not in {"ENTER", "PROTECT", "TIGHTEN", "EXIT", "FLATTEN"}:
            raise ValueError(f"unsupported intent type: {intent_type}")
        if side not in {"BUY", "SELL"}:
            raise ValueError("intent side must be BUY or SELL")
        now = now_utc().isoformat()
        payload_json = json.dumps(payload or {}, default=str, sort_keys=True)
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            existing = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is not None:
                immutable = {
                    "position_key": position_key,
                    "intent_type": intent_type,
                    "role": role,
                    "side": side,
                    "quantity": quantity,
                }
                for field, expected in immutable.items():
                    if str(existing[field]) != str(expected):
                        raise ValueError(
                            f"conflicting duplicate order intent {intent_id}: {field}"
                        )
                return self._lifecycle_row(existing)
            try:
                conn.execute(
                    """
                    INSERT INTO order_intents(
                        intent_id, position_key, trade_id, intent_type, role, side,
                        quantity, reason, payload, state, state_version, latched,
                        active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', 1, ?, 1, ?, ?)
                    """,
                    (
                        intent_id,
                        position_key,
                        trade_id,
                        intent_type,
                        role,
                        side,
                        quantity,
                        reason,
                        payload_json,
                        int(latched),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # The partial unique reduction index is the cross-process
                # serialization primitive.  A concurrent creator receives the
                # existing latched intent and must reconcile it rather than
                # racing a new full-size reduction.
                if intent_type not in {"EXIT", "FLATTEN"}:
                    raise
                winner = conn.execute(
                    """
                    SELECT * FROM order_intents
                    WHERE position_key = ? AND active = 1
                      AND intent_type IN ('EXIT', 'FLATTEN')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (position_key,),
                ).fetchone()
                if winner is None:
                    raise
                return self._lifecycle_row(winner)
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "intent_prepared",
                {
                    "position_key": position_key,
                    "intent_type": intent_type,
                    "role": role,
                    "side": side,
                    "quantity": quantity,
                    "latched": latched,
                },
            )
            row = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._lifecycle_row(row)

    def get_order_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        return self._lifecycle_row(
            conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        )

    def escalate_order_intent(self, intent_id: str, reason: str) -> Dict[str, Any]:
        """Latch hard-exit urgency without losing the original exit decision."""

        if not reason:
            raise ValueError("hard-exit escalation requires a reason")
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            row = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown lifecycle intent {intent_id}")
            intent = self._lifecycle_row(row)
            if intent["intent_type"] not in {"EXIT", "FLATTEN"}:
                raise ValueError("only a reduction intent can become a hard exit")
            if not intent["active"]:
                return intent
            payload = intent["payload"]
            if (
                intent["intent_type"] == "FLATTEN"
                and intent["latched"]
                and intent["reason"] == reason
                and payload.get("hard_exit_required")
            ):
                return intent
            payload.setdefault("initiating_reason", intent["reason"])
            payload["hard_exit_required"] = True
            recovery = payload.get("recovery_trade")
            if isinstance(recovery, dict):
                recovery["exit_market_required"] = True
                recovery["exit_reason"] = reason
            conn.execute(
                "UPDATE order_intents SET intent_type = 'FLATTEN', latched = 1, "
                "reason = ?, payload = ?, state_version = state_version + 1, updated_at = ? "
                "WHERE intent_id = ? AND state_version = ?",
                (
                    reason,
                    json.dumps(payload, default=str, sort_keys=True),
                    now_utc().isoformat(),
                    intent_id,
                    intent["state_version"],
                ),
            )
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "hard_exit_escalated",
                {"previous_reason": intent["reason"], "reason": reason},
            )
            return self._lifecycle_row(
                conn.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
            )

    def record_external_handoff(
        self, intent_id: str, recovery_trade: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist external-order fill baselines before cancelling their orders.

        This narrow amendment preserves the immutable recovery identity and
        original quantity. A failed write propagates so the caller cannot
        cancel first and lose the fill constraint across a checkpoint crash.
        """
        baselines = recovery_trade.get("external_handoff_baselines", {})
        orders = recovery_trade.get("external_handoff_orders", {})
        quantity = recovery_trade.get("quantity")
        if (
            not isinstance(baselines, dict)
            or not isinstance(orders, dict)
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("external handoff requires valid baselines and quantity")
        for order_id, baseline in baselines.items():
            order = orders.get(order_id)
            if (
                not isinstance(order_id, str)
                or not order_id
                or isinstance(baseline, bool)
                or not isinstance(baseline, int)
                or baseline < 0
                or not isinstance(order, dict)
                or str(order.get("order_id")) != order_id
                or order.get("transaction_type") not in {"BUY", "SELL"}
            ):
                raise ValueError("external handoff order/baseline is invalid")
            filled = order.get("filled_quantity", 0)
            if (
                isinstance(filled, bool)
                or not isinstance(filled, int)
                or filled < baseline
            ):
                raise ValueError("external handoff cumulative fill count is invalid")

        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            row = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown lifecycle intent {intent_id}")
            intent = self._lifecycle_row(row)
            if not intent["active"] or intent["intent_type"] not in {"EXIT", "FLATTEN"}:
                raise ValueError("external handoff requires an active reduction intent")
            payload = intent["payload"]
            recovery = payload.setdefault("recovery_trade", {})
            if not isinstance(recovery, dict):
                raise ValueError("persisted recovery trade is invalid")
            stored_baselines = recovery.setdefault("external_handoff_baselines", {})
            stored_orders = recovery.setdefault("external_handoff_orders", {})
            recovery.setdefault("quantity", quantity)
            for order_id, baseline in baselines.items():
                if (
                    order_id in stored_baselines
                    and stored_baselines[order_id] != baseline
                ):
                    raise ValueError("external handoff baseline cannot change")
                previous = stored_orders.get(order_id)
                order = orders[order_id]
                if previous:
                    for field in (
                        "transaction_type",
                        "namespace",
                        "account_id",
                        "exchange",
                        "instrument_id",
                        "tradingsymbol",
                        "product",
                    ):
                        if previous.get(field) != order.get(field):
                            raise ValueError(
                                "external handoff order identity cannot change"
                            )
                    if previous.get("filled_quantity", 0) > order.get(
                        "filled_quantity", 0
                    ):
                        continue
                    terminal = {
                        "COMPLETE",
                        "CANCELLED",
                        "REJECTED",
                        "EXPIRED",
                        "REJECTED AMO",
                    }
                    if (
                        previous.get("status") in terminal
                        and order.get("status") not in terminal
                    ):
                        continue
                stored_baselines[order_id] = baseline
                stored_orders[order_id] = dict(order)
            serialized = json.dumps(payload, default=str, sort_keys=True)
            if json.loads(row["payload"] or "{}") == payload:
                return intent
            conn.execute(
                "UPDATE order_intents SET payload = ?, "
                "state_version = state_version + 1, updated_at = ? "
                "WHERE intent_id = ? AND state_version = ?",
                (serialized, now_utc().isoformat(), intent_id, intent["state_version"]),
            )
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "external_handoff_recorded",
                {"order_ids": sorted(baselines), "quantity": recovery["quantity"]},
            )
            return self._lifecycle_row(
                conn.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
            )

    def update_protection_trigger(
        self, intent_id: str, trigger_price: float, confirmed: bool = False
    ) -> Dict[str, Any]:
        """Persist modification uncertainty separately from confirmed protection."""

        if (
            isinstance(trigger_price, bool)
            or not isinstance(trigger_price, (int, float))
            or not math.isfinite(trigger_price)
            or trigger_price <= 0
        ):
            raise ValueError("protection trigger must be finite and positive")
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            row = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown lifecycle intent {intent_id}")
            intent = self._lifecycle_row(row)
            if not intent["active"] or intent["intent_type"] not in {
                "PROTECT",
                "TIGHTEN",
            }:
                raise ValueError("trigger changes require an active protection intent")
            payload = intent["payload"]
            recovery = payload.setdefault("recovery_trade", {})
            confirmed_trigger = recovery.get("sl") or payload.get("trigger_price")
            requested_trigger = recovery.get("requested_stop_trigger")
            for baseline in (confirmed_trigger, requested_trigger):
                if baseline is None:
                    continue
                if (
                    isinstance(baseline, bool)
                    or not isinstance(baseline, (int, float))
                    or not math.isfinite(baseline)
                    or baseline <= 0
                ):
                    raise ValueError("persisted protection trigger is invalid")
                loosens = (
                    trigger_price < baseline
                    if intent["side"] == "SELL"
                    else trigger_price > baseline
                )
                if loosens:
                    raise ValueError(
                        "protection modification cannot loosen its trigger"
                    )
            if confirmed:
                recovery["sl"] = trigger_price
                recovery.pop("requested_stop_trigger", None)
                payload["trigger_price"] = trigger_price
            else:
                recovery["requested_stop_trigger"] = trigger_price
            conn.execute(
                "UPDATE order_intents SET payload = ?, "
                "state_version = state_version + 1, updated_at = ? "
                "WHERE intent_id = ? AND state_version = ?",
                (
                    json.dumps(payload, default=str, sort_keys=True),
                    now_utc().isoformat(),
                    intent_id,
                    intent["state_version"],
                ),
            )
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "protection_trigger_confirmed"
                if confirmed
                else "protection_trigger_requested",
                {"trigger_price": trigger_price, "previous_trigger": confirmed_trigger},
            )
            return self._lifecycle_row(
                conn.execute(
                    "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
                ).fetchone()
            )

    def find_active_order_intent(
        self, position_key: str, intent_types: Optional[set[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Return the newest active intent for a broker-position key.

        EXIT/FLATTEN are additionally protected by a partial unique index, so
        this projection is safe across process restarts as well as threads.
        """

        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        params: list[Any] = [position_key]
        where = "position_key = ? AND active = 1"
        if intent_types:
            normalized = sorted(str(value).upper() for value in intent_types)
            where += " AND intent_type IN (%s)" % ", ".join("?" * len(normalized))
            params.extend(normalized)
        row = conn.execute(
            f"SELECT * FROM order_intents WHERE {where} "
            "ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
        return self._lifecycle_row(row)

    def prepare_order_attempt(
        self,
        *,
        intent_id: str,
        attempt_id: str,
        attempt_tag: str,
        payload: Optional[Dict[str, Any]] = None,
        enforce_previous_attempt: bool = False,
        expected_previous_attempt_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a unique attempt tag before asking the broker to mutate.

        An active/unknown attempt is returned unchanged.  Callers must
        reconcile it, not generate another tag and submit duplicate exposure.
        """

        if not attempt_id or not attempt_tag:
            raise ValueError("attempt_id and attempt_tag are required")
        now = now_utc().isoformat()
        payload_json = json.dumps(payload or {}, default=str, sort_keys=True)
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            intent = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            if not intent["active"]:
                raise ValueError(f"order intent {intent_id} is terminal")
            existing = conn.execute(
                "SELECT * FROM order_attempts WHERE intent_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
            if (
                enforce_previous_attempt
                and (existing["attempt_id"] if existing is not None else None)
                != expected_previous_attempt_id
            ):
                if existing is None:
                    raise ValueError("the expected previous attempt is missing")
                return dict(existing)
            if (
                existing is not None
                and existing["state"] in self._ACTIVE_ATTEMPT_STATES
            ):
                return dict(existing)
            by_id = conn.execute(
                "SELECT * FROM order_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if by_id is not None:
                if (
                    by_id["intent_id"] != intent_id
                    or by_id["attempt_tag"] != attempt_tag
                ):
                    raise ValueError(
                        f"conflicting duplicate order attempt {attempt_id}"
                    )
                return dict(by_id)
            conn.execute(
                """
                INSERT INTO order_attempts(
                    attempt_id, intent_id, attempt_tag, broker_order_id, state,
                    state_version, payload, error, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'SUBMITTING', 1, ?, NULL, ?, ?)
                """,
                (attempt_id, intent_id, attempt_tag, payload_json, now, now),
            )
            next_version = int(intent["state_version"]) + 1
            conn.execute(
                """
                UPDATE order_intents
                SET state = 'SUBMITTING', state_version = ?, updated_at = ?
                WHERE intent_id = ? AND state_version = ?
                """,
                (next_version, now, intent_id, intent["state_version"]),
            )
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "attempt_submitting",
                {"attempt_tag": attempt_tag, "state_version": next_version},
                attempt_id,
            )
            return dict(
                conn.execute(
                    "SELECT * FROM order_attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
            )

    def record_order_attempt_state(
        self,
        attempt_id: str,
        state: str,
        *,
        broker_order_id: Optional[str] = None,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an acknowledgement, observation, rejection or UNKNOWN state."""

        state = str(state).upper()
        allowed = {
            "PREPARED",
            "SUBMITTING",
            "ACKNOWLEDGED",
            "WORKING",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCEL_PENDING",
            "CANCELLED",
            "REJECTED",
            "UNKNOWN",
        }
        if state not in allowed:
            raise ValueError(f"unsupported order attempt state: {state}")
        now = now_utc().isoformat()
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            attempt = conn.execute(
                "SELECT * FROM order_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise ValueError(f"unknown order attempt {attempt_id}")
            intent = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?",
                (attempt["intent_id"],),
            ).fetchone()
            if intent is None:
                raise ValueError(f"attempt {attempt_id} has no intent")
            if (
                broker_order_id
                and attempt["broker_order_id"]
                and str(broker_order_id) != str(attempt["broker_order_id"])
            ):
                raise ValueError("a broker order cannot be reassigned to an attempt")
            previous_state = attempt["state"]
            if (
                (
                    previous_state in {"FILLED", "CANCELLED", "REJECTED"}
                    and state in self._ACTIVE_ATTEMPT_STATES
                )
                or (
                    previous_state in {"WORKING", "PARTIALLY_FILLED"}
                    and state in {"PREPARED", "SUBMITTING", "ACKNOWLEDGED", "UNKNOWN"}
                )
                or (previous_state == "PARTIALLY_FILLED" and state == "WORKING")
            ):
                state = previous_state
            if previous_state == "FILLED":
                state = previous_state
            next_attempt_version = int(attempt["state_version"]) + 1
            attempt_update = conn.execute(
                """
                UPDATE order_attempts
                SET state = ?, state_version = ?,
                    broker_order_id = COALESCE(?, broker_order_id),
                    error = ?, updated_at = ?
                WHERE attempt_id = ? AND state_version = ?
                """,
                (
                    state,
                    next_attempt_version,
                    str(broker_order_id) if broker_order_id else None,
                    error,
                    now,
                    attempt_id,
                    attempt["state_version"],
                ),
            )
            if attempt_update.rowcount == 0:
                raise RuntimeError(f"concurrent lifecycle update for {attempt_id}")
            next_intent_version = int(intent["state_version"]) + 1
            newest_attempt = conn.execute(
                "SELECT attempt_id FROM order_attempts WHERE intent_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (intent["intent_id"],),
            ).fetchone()
            if intent["active"] and newest_attempt["attempt_id"] == attempt_id:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, state_version = ?, updated_at = ?
                    WHERE intent_id = ? AND state_version = ?
                    """,
                    (
                        state,
                        next_intent_version,
                        now,
                        intent["intent_id"],
                        intent["state_version"],
                    ),
                )
            self._lifecycle_event_inner(
                conn,
                intent["intent_id"],
                f"attempt_{state.lower()}",
                {
                    "broker_order_id": broker_order_id,
                    "error": error,
                    **(details or {}),
                },
                attempt_id,
            )
            resolved_order_id = broker_order_id or attempt["broker_order_id"]
            if resolved_order_id:
                self._allocate_order_fills_inner(
                    conn, intent, attempt_id, str(resolved_order_id)
                )
            row = conn.execute(
                "SELECT * FROM order_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return dict(row)

    def _allocate_order_fills_inner(
        self, conn, intent, attempt_id: str, broker_order_id: str
    ) -> None:
        """Attach fills that arrived before the attempt's broker acknowledgement."""

        namespace, account_id = self._lifecycle_account_identity(intent["position_key"])
        rows = conn.execute(
            "SELECT * FROM order_fill_ledger "
            "WHERE namespace = ? AND account_id = ? AND broker_order_id = ?",
            (namespace, account_id, broker_order_id),
        ).fetchall()
        for row in rows:
            if row["side"] != intent["side"] or (
                row["intent_id"] not in (None, intent["intent_id"])
                or row["attempt_id"] not in (None, attempt_id)
            ):
                raise ValueError("broker fill conflicts with its lifecycle owner")
            if row["intent_id"] is not None and row["attempt_id"] is not None:
                continue
            conn.execute(
                "UPDATE order_fill_ledger "
                "SET intent_id = ?, attempt_id = ?, position_key = ? "
                "WHERE namespace = ? AND account_id = ? AND broker_fill_id = ?",
                (
                    intent["intent_id"],
                    attempt_id,
                    intent["position_key"],
                    namespace,
                    account_id,
                    row["broker_fill_id"],
                ),
            )
            self._lifecycle_event_inner(
                conn,
                intent["intent_id"],
                "fill_allocated",
                {"broker_fill_id": row["broker_fill_id"]},
                attempt_id,
            )

    def record_order_fill(
        self,
        *,
        broker_fill_id: str,
        broker_order_id: str,
        position_key: str,
        side: str,
        quantity: int,
        fill_price: Optional[float] = None,
        exchange_time: Optional[datetime | str] = None,
        intent_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        raw_fill: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Idempotently retain every broker fill exactly once.

        Broker fill IDs are scoped to namespace/account. Replayed observations
        may repair missing linkage or execution metadata, but cannot overwrite
        conflicting execution facts.
        """

        if not broker_fill_id or not broker_order_id or not position_key:
            raise ValueError("fill id, order id and position key are required")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("fill quantity must be a positive integer")
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("fill side must be BUY or SELL")
        if fill_price is not None and (
            isinstance(fill_price, bool)
            or not isinstance(fill_price, (int, float))
            or not math.isfinite(fill_price)
            or fill_price <= 0
        ):
            raise ValueError("fill price must be finite and positive when known")
        namespace, account_id = self._lifecycle_account_identity(position_key)
        try:
            normalized_exchange_time = as_utc(exchange_time)
        except (TypeError, ValueError):
            normalized_exchange_time = None
        exchange_timestamp = (
            normalized_exchange_time.isoformat()
            if normalized_exchange_time is not None
            else None
        )
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            owner = self._order_attempt_by_broker_order_inner(
                conn, str(broker_order_id), position_key
            )
            if owner:
                if intent_id not in (None, owner["intent_id"]) or attempt_id not in (
                    None,
                    owner["attempt_id"],
                ):
                    raise ValueError("broker fill conflicts with its lifecycle owner")
                if side != owner["intent_side"]:
                    raise ValueError(
                        "broker fill side conflicts with its lifecycle owner"
                    )
                intent_id = owner["intent_id"]
                attempt_id = owner["attempt_id"]
                position_key = owner["owner_position_key"]
            elif intent_id is not None or attempt_id is not None:
                raise ValueError(
                    "broker fill allocation requires a known broker attempt"
                )
            existing = conn.execute(
                "SELECT * FROM order_fill_ledger "
                "WHERE namespace = ? AND account_id = ? AND broker_fill_id = ?",
                (namespace, account_id, str(broker_fill_id)),
            ).fetchone()
            if existing is not None:
                facts = {
                    "broker_order_id": str(broker_order_id),
                    "side": side,
                    "quantity": quantity,
                    "fill_price": fill_price,
                    "exchange_time": exchange_timestamp,
                    "intent_id": intent_id,
                    "attempt_id": attempt_id,
                }
                for field, value in facts.items():
                    if (
                        existing[field] is not None
                        and value is not None
                        and existing[field] != value
                    ):
                        raise ValueError(
                            f"conflicting broker fill observation: {field}"
                        )
                conn.execute(
                    "UPDATE order_fill_ledger SET "
                    "intent_id = COALESCE(intent_id, ?), "
                    "attempt_id = COALESCE(attempt_id, ?), "
                    "fill_price = COALESCE(fill_price, ?), "
                    "exchange_time = COALESCE(exchange_time, ?), "
                    "position_key = CASE WHEN ? IS NOT NULL THEN ? ELSE position_key END "
                    "WHERE namespace = ? AND account_id = ? AND broker_fill_id = ?",
                    (
                        intent_id,
                        attempt_id,
                        fill_price,
                        exchange_timestamp,
                        intent_id,
                        position_key,
                        namespace,
                        account_id,
                        str(broker_fill_id),
                    ),
                )
                return False
            cursor = conn.execute(
                """
                INSERT INTO order_fill_ledger(
                    namespace, account_id, broker_fill_id, intent_id, attempt_id, broker_order_id,
                    position_key, side, quantity, fill_price, exchange_time,
                    recorded_at, raw_fill
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    account_id,
                    str(broker_fill_id),
                    intent_id,
                    attempt_id,
                    str(broker_order_id),
                    position_key,
                    side,
                    quantity,
                    fill_price,
                    exchange_timestamp,
                    now_utc().isoformat(),
                    json.dumps(raw_fill or {}, default=str, sort_keys=True),
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and intent_id:
                self._lifecycle_event_inner(
                    conn,
                    intent_id,
                    "fill_recorded",
                    {
                        "broker_fill_id": broker_fill_id,
                        "broker_order_id": broker_order_id,
                        "quantity": quantity,
                        "fill_price": fill_price,
                    },
                    attempt_id,
                )
        return inserted

    def get_order_intent_projection(self, intent_id: str) -> Optional[Dict[str, Any]]:
        """Return the current intent, newest attempt and idempotent fill total."""

        intent = self.get_order_intent(intent_id)
        if intent is None:
            return None
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        attempt = conn.execute(
            "SELECT * FROM order_attempts WHERE intent_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (intent_id,),
        ).fetchone()
        fill_row = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS filled_quantity "
            "FROM order_fill_ledger WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        intent["latest_attempt"] = dict(attempt) if attempt is not None else None
        intent["filled_quantity"] = int(fill_row["filled_quantity"] or 0)
        intent["residual_quantity"] = max(
            0, intent["quantity"] - intent["filled_quantity"]
        )
        return intent

    def _order_attempt_by_broker_order_inner(
        self, conn, broker_order_id: str, position_key: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT a.*, i.position_key AS owner_position_key, i.side AS intent_side "
            "FROM order_attempts a JOIN order_intents i ON a.intent_id = i.intent_id "
            "WHERE a.broker_order_id = ?",
            (str(broker_order_id),),
        ).fetchall()
        if position_key is not None:
            identity = self._lifecycle_account_identity(position_key)
            rows = [
                row
                for row in rows
                if self._lifecycle_account_identity(row["owner_position_key"])
                == identity
            ]
        # An unscoped/ambiguous identity never selects whichever account wrote last.
        return self._lifecycle_row(rows[0]) if len(rows) == 1 else None

    def get_order_attempt_by_broker_order(
        self, broker_order_id: str, *, position_key: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        return self._order_attempt_by_broker_order_inner(
            conn, broker_order_id, position_key
        )

    def get_position_order_ids(self, position_key: str) -> set[str]:
        """Include terminal predecessor attempts belonging to this exact epoch."""

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT a.broker_order_id FROM order_attempts a "
            "JOIN order_intents i ON i.intent_id = a.intent_id "
            "WHERE i.position_key = ? AND a.broker_order_id IS NOT NULL",
            (position_key,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def get_terminal_order_fact(
        self, broker_order_id: str, *, namespace: str, account_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return a scoped broker terminal observation retained before rollover."""

        if namespace in (None, "", "UNKNOWN") or account_id in (None, "", "UNKNOWN"):
            return None
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT e.details, i.position_key FROM order_lifecycle_events e "
            "JOIN order_attempts a ON a.attempt_id = e.attempt_id "
            "JOIN order_intents i ON i.intent_id = a.intent_id "
            "WHERE a.broker_order_id = ? AND a.state IN ('FILLED', 'CANCELLED', 'REJECTED') "
            "ORDER BY e.timestamp DESC, e.rowid DESC",
            (str(broker_order_id),),
        ).fetchall()
        for row in rows:
            if self._lifecycle_account_identity(row["position_key"]) != (
                str(namespace),
                str(account_id),
            ):
                continue
            order = json.loads(row["details"]).get("order")
            if (
                isinstance(order, dict)
                and str(order.get("order_id", order.get("orderId")))
                == str(broker_order_id)
                and str(order.get("status", "")).upper()
                in {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED", "REJECTED AMO"}
            ):
                return order
        return None

    def get_position_fills(
        self, position_key: str, *, broker_order_ids: set[str]
    ) -> List[Dict[str, Any]]:
        """Read retained execution facts, never a claim about current broker state.

        Both epoch and explicit owned-order linkage are required. This also
        excludes unallocated v1 symbol-history observations from a new epoch.
        """

        namespace, account_id = self._lifecycle_account_identity(position_key)
        if not broker_order_ids:
            return []
        order_ids = sorted(str(order_id) for order_id in broker_order_ids)
        placeholders = ", ".join("?" for _ in order_ids)
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM order_fill_ledger WHERE namespace = ? AND account_id = ? "
            "AND position_key = ? "
            f"AND broker_order_id IN ({placeholders}) ORDER BY recorded_at, broker_fill_id",
            (namespace, account_id, position_key, *order_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_order_intent_event(
        self,
        intent_id: str,
        event_type: str,
        details: Dict[str, Any],
        attempt_id: Optional[str] = None,
    ) -> None:
        """Append non-mutating handoff/recovery evidence to an intent trace."""

        conn = self._get_conn()
        with self._transaction(conn):
            exists = conn.execute(
                "SELECT 1 FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"unknown order intent {intent_id}")
            self._lifecycle_event_inner(
                conn, intent_id, event_type, details, attempt_id
            )

    def list_unresolved_order_intents(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT intent_id FROM order_intents WHERE active = 1 "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [
            projection
            for row in rows
            if (projection := self.get_order_intent_projection(row["intent_id"]))
            is not None
        ]

    def complete_order_intent(
        self,
        intent_id: str,
        state: str = "CLOSED",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark an obligation terminal only after the coordinator proves it."""

        now = now_utc().isoformat()
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        with self._transaction(conn):
            intent = conn.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            if not intent["active"]:
                return
            next_version = int(intent["state_version"]) + 1
            conn.execute(
                """
                UPDATE order_intents
                SET state = ?, active = 0, state_version = ?, updated_at = ?
                WHERE intent_id = ? AND state_version = ?
                """,
                (state, next_version, now, intent_id, intent["state_version"]),
            )
            self._lifecycle_event_inner(
                conn,
                intent_id,
                "intent_completed",
                {"state": state, **(details or {})},
            )

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
