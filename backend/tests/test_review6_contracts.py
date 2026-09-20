"""Sixth-review admission and lifecycle regressions through synthetic SDK reads."""

from copy import deepcopy
from datetime import timedelta

import pytest

from backend.config import config_manager
from backend.financial_eligibility import verified_outcome
from backend.tests.test_phase1_review_corrections import _snapshot
from backend.tests.test_review5_admission import order, position
from backend.tests.test_review5_admission import risk as risk
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.tests.test_review5_lifecycle import unknown_entry
from backend.time_utils import EXCHANGE_TIMEZONE, as_utc, now_utc
from backend.trading_engine import TradingEngine


@pytest.mark.parametrize("field", ["entryPrice", "stopLoss", "target"])
@pytest.mark.parametrize(
    "value",
    ["110", True, False, None, float("nan"), float("inf"), -float("inf"), 0, -1],
)
def test_execute_signal_rejects_noncanonical_prices_before_submission(
    lifecycle, field, value
):
    e = lifecycle
    assert e.engine.execute_signal({**e.signal, field: value}) is False
    assert e.sdk.calls == []
    assert e.engine.active_trades == {}
    assert e.risk.pending_entry_reservation_count == 0


@pytest.mark.parametrize("legacy_target", [False, True])
def test_admitted_numeric_prices_and_legacy_bad_target_do_not_strand_hard_stops(
    lifecycle, monkeypatch, legacy_target
):
    e = lifecycle
    e.sdk.after_entry = e.sdk.fill_entry
    e.signal.update(entryPrice=100.03, stopLoss=95.0)
    assert e.engine.execute_signal(e.signal) is True
    trade = e.engine.active_trades["RELIANCE"]
    assert type(trade["sl"]) is float
    assert type(trade["target"]) is float
    assert e.sdk.calls[0]["price"] == 100.05
    assert e.journal.get_trades()[0]["signal_entry_price"] == 100.05
    assert e.journal.get_trades()[0]["target"] == 110
    persisted = config_manager.load_active_trades()["RELIANCE"]
    assert persisted["sl"] == 95
    assert persisted["target"] == 110
    if legacy_target:
        trade["target"] = "110"
    e.sdk.position_rows.append(
        {**e.sdk.position_rows[0], "tradingsymbol": "INFY", "instrument_token": 222}
    )
    e.engine.active_trades["INFY"] = {
        **trade,
        "tradingsymbol": "INFY",
        "instrument_id": "222",
        "target": 110,
        "trade_id": None,
        "stop_order_id": None,
    }
    e.sdk.quote = lambda instruments: {
        name: {"last_price": 90, "timestamp": now_utc()} for name in instruments
    }
    exits = []
    monkeypatch.setattr(
        e.engine,
        "_place_exit_order",
        lambda p, symbol, reason: exits.append((symbol, reason)),
    )
    e.engine.monitor_positions()
    assert exits == [("RELIANCE", "Stop Loss"), ("INFY", "Stop Loss")]


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("reduced", [200, 400, 800])
@pytest.mark.parametrize("proposal_quantity", [800, 1000, 1200])
def test_reducers_are_gross_exempt_but_all_independent_net_outcomes_are_checked(
    risk, monkeypatch, direction, reduced, proposal_quantity
):
    config = dict(config_manager.get_risk_config(), maxGrossExposure=200000)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    sign = 1 if direction == "BUY" else -1
    opposite = "SELL" if sign == 1 else "BUY"
    stop = order("STOP", side=opposite, quantity=reduced, filled=0, role="PROTECTION")
    # Trigger/limit proceeds must not change the marked value of the hedge.
    stop.update(price=1 if sign == 1 else 999, average_price=0)
    snapshot = _snapshot(
        positions=[
            position(sign * 800, 800 if sign == 1 else 0, 800 if sign == -1 else 0)
        ],
        orders=[stop],
    )
    state, reason = risk._build_exposure_state(snapshot)
    assert reason == "OK"
    assert state.gross == 80000
    assert state.pending_sells == (reduced * 100 if sign == 1 else 0)
    assert state.pending_buys == (reduced * 100 if sign == -1 else 0)
    scenarios = [
        sign
        * (
            80000
            - stop_fills * reduced * 100
            - proposal_fills * proposal_quantity * 100
        )
        for stop_fills in (0, 1)
        for proposal_fills in (0, 1)
    ]
    expected = max(abs(value) for value in scenarios) <= 100000
    accepted, reason = risk.can_accept_position(
        "INFY", opposite, proposal_quantity, 100, snapshot
    )
    assert accepted is expected, reason
    if not expected:
        assert reason.startswith("NET_EXPOSURE_LIMIT")


def test_existing_net_breach_cannot_rely_on_proposal_or_reducer_filling(risk):
    snapshot = _snapshot(
        positions=[position(1200, 1200)],
        orders=[order("STOP", side="SELL", quantity=1200, filled=0, role="PROTECTION")],
    )
    accepted, reason = risk.can_accept_position("INFY", "SELL", 800, 100, snapshot)
    assert not accepted
    assert reason.startswith("NET_EXPOSURE_LIMIT")


@pytest.mark.parametrize("position_row", ["missing", "zero_stale", "reflected"])
@pytest.mark.parametrize(
    "quote_kind", ["missing", "failed", "stale", "untimed", "invalid", "fresh"]
)
def test_reconstructed_residual_requires_fresh_market_mark(
    lifecycle, position_row, quote_kind
):
    e = lifecycle
    config_manager.get_risk_config().update(
        maxGrossExposure=150000, maxNetExposure=500000
    )
    e.risk._get_correlation = lambda *_: 0
    old = now_utc() - timedelta(hours=3)
    e.sdk.book = [order("ENTRY", quantity=800, filled=800, status="COMPLETE")]
    e.sdk.executions = [
        {
            "trade_id": "F-ENTRY",
            "order_id": "ENTRY",
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 111,
            "transaction_type": "BUY",
            "quantity": 800,
            "average_price": 100,
            "fill_timestamp": old,
        }
    ]
    if position_row != "missing":
        e.sdk.position_rows = [
            position(
                800 if position_row == "reflected" else 0,
                800 if position_row == "reflected" else 0,
            )
        ]
        e.sdk.position_rows[0]["timestamp"] = old

    def quote(instruments):
        if quote_kind == "failed":
            raise TimeoutError("offline quote fault")
        if quote_kind == "missing":
            return {}
        return {
            name: {
                "last_price": True if quote_kind == "invalid" else 150,
                "timestamp": None
                if quote_kind == "untimed"
                else old
                if quote_kind == "stale"
                else now_utc(),
            }
            for name in instruments
        }

    e.sdk.quote = quote
    snapshot = e.client.get_broker_snapshot()
    state, reason = e.risk._build_exposure_state(snapshot)
    accepted, admission_reason = e.risk.can_accept_position(
        "INFY", "BUY", 500, 100, snapshot
    )
    assert not accepted
    if position_row != "missing" and quote_kind == "fresh":
        assert state.gross == 120000
        assert snapshot.positions[0].last_price == 150
        assert (now_utc() - snapshot.positions[0].mark_time).total_seconds() < 5
        assert admission_reason.startswith("GROSS_EXPOSURE_LIMIT")
    else:
        assert state is None
        assert reason.startswith(("MARK_UNAVAILABLE", "BROKER_STATE_UNAVAILABLE"))
        if position_row in {"missing", "zero_stale"}:
            assert snapshot.entry_ready
            assert reason.startswith("MARK_UNAVAILABLE")
    # A later coherent row and current quote restore admission at the actual
    # mark; no fill/limit price gets a newly fabricated observation timestamp.
    e.sdk.position_rows = [position(800, 800)]
    e.sdk.quote = lambda instruments: {
        name: {"last_price": 150, "timestamp": now_utc()} for name in instruments
    }
    reflected = e.client.get_broker_snapshot()
    assert e.risk._build_exposure_state(reflected)[0].gross == 120000
    assert e.risk.can_accept_position("INFY", "BUY", 200, 100, reflected)[0]


def test_normal_entry_stop_filled_during_confluence_uses_execution_time(
    lifecycle, monkeypatch
):
    import backend.trading_engine as engine_module

    e = lifecycle
    e.sdk.after_entry = e.sdk.fill_entry

    def confluence(*args):
        # Broker timestamps are only second precision; bookkeeping follows
        # both fills within that second, recreating the original cutoff bug.
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "STOP-FILL",
                "order_id": "O2",
                "transaction_type": "SELL",
                "average_price": 95,
            }
        )
        e.sdk.book[1].update(status="COMPLETE", filled_quantity=10, pending_quantity=0)
        e.sdk.position_rows[0].update(quantity=0, day_sell_quantity=10)
        return {}

    monkeypatch.setattr(engine_module.scanner, "evaluate_position", confluence)
    assert e.engine.execute_signal(e.signal)
    entry_time = as_utc(e.sdk.executions[0]["fill_timestamp"])
    trade = e.engine.active_trades["RELIANCE"]
    row = e.journal.get_trades()[0]
    assert as_utc(trade["entry_time"]) == as_utc(row["entry_time"]) == entry_time
    assert as_utc(row["entry_observed_at"]) > entry_time
    for record in (trade, row, {**row, "entry_time": now_utc().isoformat()}):
        price, reason, exit_time, costs = e.engine._reconcile_execution(
            "RELIANCE", record
        )
        assert (price, reason) == (95, "stop_loss")
        assert as_utc(exit_time) == entry_time
        assert costs["financial_quality"] == "RECONCILED"
    TradingEngine()._reconcile_journal_trades()
    closed = e.journal.get_trade(row["id"])
    assert closed["quantity"] == 10
    assert closed["gross_pnl"] == -60
    assert closed["exit_price"] == 95
    assert verified_outcome(closed)
    e.engine._external_close_grace_seconds = 0
    e.engine.monitor_positions()
    e.engine.monitor_positions()
    assert e.engine.active_trades == {}


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("missing_time", [False, True])
def test_entry_execution_time_is_consistent_or_explicitly_unknown(
    lifecycle, recovery, missing_time
):
    e = lifecycle
    first_time = now_utc() - timedelta(minutes=3)

    def fill():
        e.sdk.fill_entry()
        e.sdk.executions[0].update(quantity=4, fill_timestamp=first_time)
        e.sdk.executions.append(
            {
                **e.sdk.executions[0],
                "trade_id": "F2",
                "quantity": 6,
                "fill_timestamp": None
                if missing_time
                else first_time + timedelta(seconds=5),
            }
        )

    if recovery:
        unknown_entry(e)
        fill()
        e.engine.monitor_positions()
    else:
        e.sdk.after_entry = fill
        assert e.engine.execute_signal(e.signal)
    row = e.journal.get_trades()[0]
    trade = e.engine.active_trades["RELIANCE"]
    expected = None if missing_time else first_time
    assert as_utc(trade["entry_time"]) == as_utc(row["entry_time"]) == expected
    assert row["entry_observed_at"] is not None
    assert e.journal.get_todays_trade_counts()["total"] == 1
    e.engine._persist_trades()
    restarted = TradingEngine()
    restarted.reconcile_active_trades()
    assert as_utc(restarted.active_trades["RELIANCE"]["entry_time"]) == expected


def mixed_exit(e, monkeypatch, *, missing_time=False, predecessors=False):
    first = (
        now_utc()
        .astimezone(EXCHANGE_TIMEZONE)
        .replace(hour=10, minute=0, second=0, microsecond=0)
    )
    e.sdk.after_entry = lambda: (
        e.sdk.fill_entry(),
        e.sdk.executions[0].update(fill_timestamp=first),
    )
    assert e.engine.execute_signal(e.signal)
    e.sdk.position_rows[0].update(quantity=6, day_sell_quantity=4)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "MANUAL-PART",
            "order_id": "MANUAL",
            "transaction_type": "SELL",
            "quantity": 4,
            "average_price": 108,
            "fill_timestamp": first + timedelta(minutes=5),
        }
    )
    e.sdk.cancel_order = lambda variety, order_id, **kw: next(
        o.update(status="CANCELLED", pending_quantity=0)
        for o in e.sdk.book
        if o["order_id"] == order_id
    )
    e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    if predecessors:
        e.sdk.book[2].update(status="CANCELLED", filled_quantity=2, pending_quantity=0)
        e.sdk.position_rows[0].update(quantity=4, day_sell_quantity=6)
        e.sdk.executions.append(
            {
                **e.sdk.executions[-1],
                "trade_id": "EXIT-PART",
                "order_id": "O3",
                "quantity": 2,
                "average_price": 110,
                "fill_timestamp": first + timedelta(minutes=10),
            }
        )
        e.engine._sync_exit_pending_status("RELIANCE")
        e.engine._place_exit_order(e.engine._positions()[0], "RELIANCE", "Target")
    final_id = e.sdk.book[-1]["order_id"]
    e.sdk.book[-1].update(
        status="COMPLETE", filled_quantity=4 if predecessors else 6, pending_quantity=0
    )
    e.sdk.executions.append(
        {
            **e.sdk.executions[-1],
            "trade_id": "EXIT-FINAL",
            "order_id": final_id,
            "quantity": 4 if predecessors else 6,
            "average_price": 110,
            "fill_timestamp": None if missing_time else first + timedelta(minutes=15),
        }
    )
    e.sdk.position_rows[0].update(
        quantity=0,
        day_sell_quantity=10,
        sell_quantity=10,
        sell_value=1092,
        realised=82,
        unrealised=0,
    )
    return first


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("predecessors", [False, True])
def test_mixed_exits_use_latest_execution_and_real_gateway_cooldown(
    lifecycle, monkeypatch, reverse, predecessors
):
    import backend.execution_gateway as gateway_module

    e = lifecycle
    first = mixed_exit(e, monkeypatch, predecessors=predecessors)
    if reverse:
        e.sdk.executions.reverse()
    assert e.engine._journal_external_close("RELIANCE")
    row = e.journal.get_trades()[0]
    assert row["exit_price"] == 109.2
    assert as_utc(row["exit_time"]) == first + timedelta(minutes=15)
    assert verified_outcome(row)
    assert e.journal.get_last_exit_time("RELIANCE") == first + timedelta(minutes=15)
    TradingEngine()._reconcile_journal_trades()
    e.engine.active_trades.clear()
    monkeypatch.setattr(
        gateway_module, "now_utc", lambda: first + timedelta(minutes=20)
    )
    before = deepcopy(e.sdk.calls)
    assert e.engine.execute_signal(e.signal) is False
    assert e.sdk.calls == before
    assert e.risk.pending_entry_reservation_count == 0
    monkeypatch.setattr(
        gateway_module, "now_utc", lambda: first + timedelta(minutes=31)
    )
    # Prove that cooldown, not another admission gate, blocked the earlier attempt.
    reservation_id, reason = e.risk.reserve_entry(
        "RELIANCE", "BUY", 10, 100, e.client.get_broker_snapshot()
    )
    assert reservation_id, reason
    gateway_module.execution_gateway.place_order(
        is_entry=True,
        entry_reservation_id=reservation_id,
        variety="regular",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="BUY",
        quantity=10,
        product="MIS",
        order_type="LIMIT",
        price=100,
    )
    assert len(e.sdk.calls) == len(before) + 1


@pytest.mark.parametrize("invalid_time", [None, "invalid-timestamp"])
def test_timestamp_incomplete_close_remains_pending_until_repaired(
    lifecycle, monkeypatch, invalid_time
):
    e = lifecycle
    first = mixed_exit(e, monkeypatch)
    e.sdk.executions[-1]["fill_timestamp"] = invalid_time
    assert not e.engine._journal_external_close("RELIANCE")
    row = e.journal.get_trades()[0]
    assert row["status"] == "RECONCILIATION_PENDING"
    assert row["exit_time"] is None
    assert not verified_outcome(row)
    with pytest.raises(ValueError, match="Exit execution time unresolved"):
        e.journal.get_last_exit_time("RELIANCE")
    before = deepcopy(e.sdk.calls)
    e.engine.active_trades.clear()
    assert not e.engine.execute_signal(e.signal)
    assert e.sdk.calls == before
    e.sdk.executions[-1]["fill_timestamp"] = first + timedelta(minutes=15)
    TradingEngine()._reconcile_journal_trades()
    repaired = e.journal.get_trade(row["id"])
    assert repaired["exit_price"] == 109.2
    assert as_utc(repaired["exit_time"]) == first + timedelta(minutes=15)
    assert verified_outcome(repaired)


def test_missing_mark_blocks_entries_but_preserves_recovery_protection(lifecycle):
    import backend.execution_gateway as gateway_module

    e = lifecycle
    unknown_entry(e)
    e.sdk.fill_entry()
    e.sdk.quote = lambda instruments: {}
    e.engine.monitor_positions()
    assert e.sdk.calls[-1]["order_type"] == "SL"
    assert e.sdk.calls[-1]["quantity"] == 10
    state, reason = e.risk._build_exposure_state(e.client.get_broker_snapshot())
    assert state is None
    assert reason == "BROKER_STATE_UNAVAILABLE"
    gateway_module.execution_gateway.emergency_flatten_position(
        variety="regular",
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="SELL",
        quantity=10,
        product="MIS",
        order_type="MARKET",
    )
    assert e.sdk.calls[-1]["order_type"] == "MARKET"
    assert e.sdk.calls[-1]["transaction_type"] == "SELL"


@pytest.mark.parametrize("unlinked_time", ["before_entry", "missing", "unknown_entry"])
def test_unlinked_fills_still_require_a_known_entry_window(
    lifecycle, monkeypatch, unlinked_time
):
    e = lifecycle
    first = mixed_exit(e, monkeypatch)
    if unlinked_time == "before_entry":
        e.sdk.executions[1]["fill_timestamp"] = first - timedelta(minutes=1)
    elif unlinked_time == "missing":
        e.sdk.executions[1]["fill_timestamp"] = None
    else:
        e.sdk.executions[0]["fill_timestamp"] = None
        e.engine.active_trades["RELIANCE"]["entry_time"] = None
    result = e.engine._reconcile_execution(
        "RELIANCE", e.engine.active_trades["RELIANCE"]
    )
    assert result == (None, "UNRECONCILED", None, None)
