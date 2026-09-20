import sqlite3

import pytest

from backend.analytics import TradeAnalytics
from backend.calibration import ProbabilityCalibrator
from backend.financial_eligibility import verified_outcome, verified_outcome_sql
from backend.journal import TradeJournal
from backend.time_utils import as_utc, now_utc


def _open(journal, trade_id="T1", product="MIS"):
    journal.open_trade(
        trade_id=trade_id,
        tradingsymbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        product=product,
        strategy="test",
        entry_price=100,
        quantity=10,
        stop_loss=95,
        target=110,
    )


def test_estimated_financials_are_marked_and_excluded_from_research(tmp_path):
    path = tmp_path / "journal.db"
    journal = TradeJournal(str(path))
    _open(journal)
    journal.close_trade(
        "T1",
        110,
        "target",
        cost_details={
            "gross_pnl": 100,
            "net_pnl": 90,
            "brokerage": 5,
            "taxes": 3,
            "exchange_charges": 2,
            "other_fees": 0,
            "slippage": 0,
            "financial_quality": "ESTIMATED",
            "financial_provenance": "projection",
        },
    )
    row = journal.get_trades()[0]
    assert row["financial_quality"] == "ESTIMATED"
    assert row["financial_provenance"] == "projection"
    assert TradeAnalytics(str(path)).get_strategy_expectancy() == []


def test_negative_fee_details_are_rejected_but_negative_pnl_is_valid(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    with pytest.raises(ValueError, match="brokerage cannot be negative"):
        journal.close_trade(
            "T1",
            90,
            "stop",
            cost_details={
                "gross_pnl": -100,
                "net_pnl": -101,
                "brokerage": -1,
                "taxes": 0,
                "exchange_charges": 0,
                "other_fees": 0,
                "slippage": 0,
            },
        )


def test_unsupported_product_is_not_silently_priced_as_mis(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal, product="CNC")
    with pytest.raises(ValueError, match="unsupported or malformed financial inputs"):
        journal.close_trade("T1", 110, "target")


def test_legacy_zero_price_closed_rows_are_quarantined(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trades (
          id TEXT PRIMARY KEY, entry_time TEXT, status TEXT, exit_price REAL,
          exit_reason TEXT
        );
        INSERT INTO trades VALUES ('T1', '2026-09-20 09:15:00', 'CLOSED', 0, 'UNRECONCILED');
        """
    )
    conn.commit()
    conn.close()

    journal = TradeJournal(str(path))
    row = journal.get_trades()[0]
    assert row["status"] == "RECONCILIATION_PENDING"
    assert row["financial_quality"] == "UNAVAILABLE"


def test_open_trade_rolls_back_row_when_its_event_write_fails(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path / "journal.db"))

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected event fault")

    monkeypatch.setattr(journal, "_log_event_inner", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        _open(journal)
    assert journal.get_trades() == []


def test_equivalent_open_trade_repairs_missing_execution_linkage(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    journal.open_trade(
        trade_id="T1",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        product="MIS",
        strategy="test",
        entry_price=100,
        quantity=10,
        stop_loss=95,
        target=110,
    )
    journal.open_trade(
        trade_id="T1",
        tradingsymbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        product="MIS",
        strategy="test",
        entry_price=100,
        quantity=10,
        stop_loss=95,
        target=110,
        entry_order_id="ENTRY1",
        stop_order_id="STOP1",
    )
    row = journal.get_trades()[0]
    assert row["entry_order_id"] == "ENTRY1"
    assert row["stop_order_id"] == "STOP1"
    assert len(journal.get_trade_events("T1")) == 1


@pytest.mark.parametrize("operation", ["close", "pending", "repair"])
def test_exit_state_and_event_rollback_then_retry(tmp_path, monkeypatch, operation):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    if operation == "repair":
        journal.close_trade("T1", None, "UNRECONCILED", "2026-09-20T06:00:00Z")
    before = journal.get_trade("T1")
    events = journal.get_trade_events("T1")
    original = journal._log_event_inner

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("event failure")

    def update():
        if operation == "repair":
            journal.update_trade_exit("T1", 105, "target", "2026-09-20T06:00:00Z")
        else:
            journal.close_trade(
                "T1",
                None if operation == "pending" else 105,
                "target",
                "2026-09-20T06:00:00Z",
            )

    monkeypatch.setattr(journal, "_log_event_inner", fail)
    with pytest.raises(sqlite3.OperationalError):
        update()
    assert journal.get_trade("T1") == before
    assert journal.get_trade_events("T1") == events
    monkeypatch.setattr(journal, "_log_event_inner", original)
    update()
    after = journal.get_trade("T1")
    assert after["status"] == (
        "RECONCILIATION_PENDING" if operation == "pending" else "CLOSED"
    )
    assert after["gross_pnl"] == (None if operation == "pending" else 50)
    assert len(journal.get_trade_events("T1")) == len(events) + 1


def test_unverified_replacement_cannot_overwrite_current_linkage(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    journal.update_execution_linkage(
        "T1", entry_order_id="ENTRY", stop_order_id="STOP1"
    )
    for update in ({"stop_order_id": "STOP2"}, {"entry_order_id": "OTHER"}):
        with pytest.raises(ValueError, match="conflicting execution linkage"):
            journal.update_execution_linkage("T1", **update)
    assert journal.get_trade("T1")["stop_order_id"] == "STOP1"


def test_duplicate_entry_repairs_execution_time_without_duplicate_entry_event(
    tmp_path,
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    observed_at = journal.get_trade("T1")["entry_observed_at"]
    entry = {
        "trade_id": "T1",
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "direction": "BUY",
        "product": "MIS",
        "strategy": "test",
        "entry_price": 100,
        "quantity": 10,
        "stop_loss": 95,
        "target": 110,
        "entry_order_id": "ENTRY",
        "entry_time": "2026-09-20T10:00:00+05:30",
    }
    journal.open_trade(**entry)
    journal.open_trade(**entry)
    row = journal.get_trade("T1")
    assert row["entry_time"] == "2026-09-20T04:30:00+00:00"
    assert row["entry_observed_at"] == observed_at
    assert [event["event_type"] for event in journal.get_trade_events("T1")] == [
        "entry_filled",
        "entry_time_reconciled",
    ]


def test_entry_time_repair_is_anchored_atomic_and_idempotent(tmp_path, monkeypatch):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    journal.update_execution_linkage("T1", entry_order_id="ENTRY")
    entry_time = "2026-09-20T10:00:00+05:30"
    before = journal.get_trade("T1")
    events = journal.get_trade_events("T1")
    original = journal._log_event_inner

    with pytest.raises(ValueError, match="conflicting entry order linkage"):
        journal.reconcile_entry_time(
            "T1", entry_order_id="OTHER", entry_time=entry_time
        )

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected entry time event fault")

    monkeypatch.setattr(journal, "_log_event_inner", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        journal.reconcile_entry_time(
            "T1", entry_order_id="ENTRY", entry_time=entry_time
        )
    assert journal.get_trade("T1") == before
    assert journal.get_trade_events("T1") == events
    monkeypatch.setattr(journal, "_log_event_inner", original)
    journal.reconcile_entry_time("T1", entry_order_id="ENTRY", entry_time=entry_time)
    journal.reconcile_entry_time(
        "T1", entry_order_id="ENTRY", entry_time="2026-09-20T04:30:00Z"
    )
    assert as_utc(journal.get_trade("T1")["entry_time"]) == as_utc(entry_time)
    assert len(journal.get_trade_events("T1")) == len(events) + 1


@pytest.mark.parametrize("entry_time", [None, "malformed", 42])
def test_unknown_entry_time_retains_daily_counts_and_realized_loss(
    tmp_path, entry_time
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    journal._get_conn().execute(
        "UPDATE trades SET entry_time = ? WHERE id = 'T1'", (entry_time,)
    )
    journal.close_trade(
        "T1",
        90,
        "stop_loss",
        now_utc().isoformat(),
        cost_details={
            "gross_pnl": -100,
            "net_pnl": -105,
            "brokerage": 2,
            "taxes": 2,
            "exchange_charges": 1,
            "other_fees": 0,
            "slippage": 0,
            "financial_quality": "RECONCILED",
            "financial_provenance": "order_grouped_broker_fills",
        },
    )
    assert journal.get_todays_trade_counts()["total"] == 1
    outcomes = journal.get_verified_todays_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["net_pnl"] == -105
    assert outcomes[0]["entry_time"] == entry_time
    analytics = TradeAnalytics(str(journal.db_path)).get_strategy_expectancy()
    assert analytics[0]["total_trades"] == 1


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("field", ["entry_price", "exit_price", "net_pnl"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), None])
def test_financial_eligibility_excludes_nonfinite_legacy_values(modern, field, value):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trades (status TEXT, entry_price REAL, exit_price REAL, net_pnl REAL"
        + (", financial_quality TEXT" if modern else "")
        + ")"
    )
    columns = ["status", "entry_price", "exit_price", "net_pnl"]
    values = ["CLOSED", 100, 110, 95]
    if modern:
        columns.append("financial_quality")
        values.append("RECONCILED")
    values[columns.index(field)] = value
    conn.execute(
        f"INSERT INTO trades VALUES ({', '.join('?' for _ in values)})", values
    )
    row = dict(conn.execute("SELECT * FROM trades").fetchone())
    assert not verified_outcome(row)
    assert (
        conn.execute(
            "SELECT * FROM trades WHERE " + verified_outcome_sql(columns)
        ).fetchall()
        == []
    )
    conn.close()


def test_missing_modern_quality_is_not_legacy_verified_evidence():
    row = {
        "status": "CLOSED",
        "entry_price": 100,
        "exit_price": 110,
        "net_pnl": 95,
        "financial_quality": None,
    }
    assert not verified_outcome(row)
    del row["financial_quality"]
    assert verified_outcome(row)


def test_corrupt_legacy_outcome_cannot_complete_calibration_sample(tmp_path):
    path = tmp_path / "journal.db"
    journal = TradeJournal(str(path))
    for index in range(10):
        _open(journal, trade_id=f"T{index}")
    journal._get_conn().execute(
        """UPDATE trades SET status = 'CLOSED', confidence = 80,
           exit_price = 110, net_pnl = 95, financial_quality = 'RECONCILED'"""
    )
    journal._get_conn().execute(
        "UPDATE trades SET net_pnl = ? WHERE id = 'T9'", (float("inf"),)
    )
    assert ProbabilityCalibrator(str(path)).get_probability("test", 80) == (None, 9)
    assert TradeAnalytics(str(path)).get_strategy_expectancy()[0]["total_trades"] == 9


def test_failed_commit_rolls_back_and_allows_entry_time_repair_retry(
    tmp_path, monkeypatch
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    _open(journal)
    journal.update_execution_linkage("T1", entry_order_id="ENTRY")
    conn = journal._get_conn()
    before = journal.get_trade("T1")
    events = journal.get_trade_events("T1")

    class CommitFailure:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    monkeypatch.setattr(journal, "_get_conn", lambda: CommitFailure())
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        journal.reconcile_entry_time(
            "T1", entry_order_id="ENTRY", entry_time="2026-09-20T04:30:00Z"
        )
    monkeypatch.setattr(journal, "_get_conn", lambda: conn)
    assert not conn.in_transaction
    assert journal.get_trade("T1") == before
    assert journal.get_trade_events("T1") == events
    journal.reconcile_entry_time(
        "T1", entry_order_id="ENTRY", entry_time="2026-09-20T04:30:00Z"
    )
    assert journal.get_trade("T1")["entry_time"] == "2026-09-20T04:30:00+00:00"
