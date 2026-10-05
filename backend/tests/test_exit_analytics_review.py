"""Operator and research projections must preserve retained trading facts."""

import json
from datetime import timedelta

import pytest

from backend.analytics import TradeAnalytics
from backend.journal import journal
from backend.tests.exit_management.test_engine import _context
from backend.tests.test_exit_live_integration import START, _managed_engine, _position


def _assessed(monkeypatch, *, close=98.0):
    import backend.trading_engine as engine_module

    engine, thesis = _managed_engine()
    context = _context(START, close=close)
    monkeypatch.setattr(engine_module, "now_utc", lambda: START + timedelta(minutes=5))
    monkeypatch.setattr(
        engine_module.scanner, "get_market_context", lambda *a, **k: context
    )
    monkeypatch.setattr(engine, "_get_tick_size", lambda *a: 0.05)
    engine._evaluate_shadow_position(
        _position(close, START + timedelta(minutes=5)), engine.active_trades["RELIANCE"]
    )
    return engine, thesis, TradeAnalytics(str(journal.db_path))


def _trade(thesis):
    return {
        "id": thesis.trade_id,
        "direction": "BUY",
        "tradingsymbol": "RELIANCE",
        "status": "CLOSED",
        "financial_quality": "RECONCILED",
        "financial_provenance": "verified_fixture_fills",
        "entry_price": 100,
        "exit_price": 105,
        "stop_loss": 102,
        "quantity": 4,
        "gross_pnl": 50,
        "net_pnl": 45,
        "entry_time": START.isoformat(),
        "exit_time": (START + timedelta(minutes=10)).isoformat(),
        "exit_reason": "BROKER_EXTERNAL_CLOSE",
    }


def test_shadow_hold_never_becomes_actual_exit_reason_or_latency(monkeypatch):
    _, thesis, analytics = _assessed(monkeypatch)
    result = analytics._exit_quality_record(journal, _trade(thesis))
    assert result["eligible"]
    assert result["reason_code"] is None
    assert result["initiating_decision_id"] is None
    assert result["metrics"]["decision_to_intent_seconds"] is None
    assert result["execution_outcome_code"] == "BROKER_EXTERNAL_CLOSE"
    assert result["action_distribution"] == {"HOLD": 1}


def test_frozen_fill_risk_survives_trailing_stop_and_residual_quantity(monkeypatch):
    _, thesis, analytics = _assessed(monkeypatch)
    result = analytics._exit_quality_record(journal, _trade(thesis))
    assert result["metrics"]["initial_risk_currency"] == 50
    assert result["metrics"]["captured_gross_r"] == 1
    assert result["metrics"]["captured_net_r"] == 0.9


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "OPEN"},
        {"exit_price": 0},
        {"financial_provenance": "legacy_zero_price_placeholder"},
        {"net_pnl": float("nan")},
    ],
)
def test_quality_flag_alone_does_not_admit_invalid_outcomes(monkeypatch, changes):
    _, thesis, analytics = _assessed(monkeypatch)
    result = analytics._exit_quality_record(journal, {**_trade(thesis), **changes})
    assert not result["eligible"]


def test_real_shadow_replay_and_actual_protection_are_distinct(monkeypatch):
    _, thesis, analytics = _assessed(monkeypatch, close=94)
    replay = analytics.get_exit_management_replay(thesis.trade_id)
    assert replay["available"]
    assert replay["exact_replay_complete"]
    assert replay["verification"][0]["status"] == "REPRODUCED"
    assert {"attempts", "fills", "execution_events"} <= replay.keys()
    panel = analytics.get_active_position_explanations()[0]
    assert panel["policy_mode"] == "shadow"
    assert panel["exposure"] == "OPEN"
    assert panel["residual_quantity"] == 10
    assert panel["pending_intent"] is None
    assert panel["protection"]["confirmed_stop"] == 95
    assert panel["protection"]["requested_stop"] is None
    assert panel["latest_decision"]["action"] == "REQUEST_EXIT"
    assert panel["thesis"]["original_boundary"] == 99.5
    assert panel["quality"]["primary"] == "VALID"


def test_no_assessment_does_not_claim_replay_inputs_available():
    _, thesis = _managed_engine()
    analytics = TradeAnalytics(str(journal.db_path))
    replay = analytics.get_exit_management_replay(thesis.trade_id)
    assert not replay["available"]
    assert not replay["exact_replay_complete"]
    result = analytics._exit_quality_record(journal, _trade(thesis))
    assert not result["retained_input_available"]
    assert result["replay_status"] == "RETAINED_INPUTS_UNAVAILABLE"


def test_corrupt_retained_input_is_reported_without_crashing_all_replay(monkeypatch):
    _, thesis, analytics = _assessed(monkeypatch)
    row = journal.get_exit_decisions(thesis.position_key)[0]
    row["payload"]["trace"]["input_snapshot"]["context"] = []
    journal._get_conn().execute(
        "UPDATE exit_decision_records SET payload = ? WHERE decision_id = ?",
        (json.dumps(row["payload"]), row["decision_id"]),
    )
    replay = analytics.get_exit_management_replay(thesis.trade_id)
    assert not replay["exact_replay_complete"]
    assert replay["verification"][0]["status"] == "REPLAY_MISMATCH"


def test_active_projection_does_not_load_full_decision_history(monkeypatch):
    _, _, analytics = _assessed(monkeypatch)
    reader = analytics._exit_journal()
    monkeypatch.setattr(
        reader, "get_exit_decisions", lambda *_: pytest.fail("unbounded history")
    )
    assert len(analytics.get_active_position_explanations()) == 1


@pytest.mark.parametrize("field", ["trace", "context", "risk", "profit_context"])
def test_corrupt_nested_documents_leave_other_operator_records_readable(
    monkeypatch, field
):
    _, thesis, analytics = _assessed(monkeypatch)
    row = journal.get_exit_decisions(thesis.position_key)[0]
    if field == "trace":
        row["payload"][field] = ["damaged"]
    else:
        row["payload"]["trace"][field] = ["damaged"]
    journal._get_conn().execute(
        "UPDATE exit_decision_records SET payload = ? WHERE decision_id = ?",
        (json.dumps(row["payload"]), row["decision_id"]),
    )
    result = analytics._exit_quality_record(journal, _trade(thesis))
    assert result["initiating_decision_id"] is None
    panel = analytics.get_active_position_explanations()[0]
    assert panel["quality"]["decision_corrupt"] is True
    assert panel["exposure"] == "OPEN"


def test_rejected_orphan_intent_cannot_explain_a_later_external_close(monkeypatch):
    _, thesis, analytics = _assessed(monkeypatch)
    journal.create_order_intent(
        intent_id="orphan-exit",
        position_key=thesis.position_key,
        trade_id=thesis.trade_id,
        intent_type="EXIT",
        role="REDUCTION",
        side="SELL",
        quantity=10,
        reason="THESIS_BREAKOUT_FAILED",
    )
    journal.complete_order_intent("orphan-exit", state="REJECTED")
    result = analytics._exit_quality_record(journal, _trade(thesis))
    assert result["reason_code"] is None
    assert result["attribution_quality"] == "UNKNOWN"
    assert result["metrics"]["intent_to_fill_seconds"] is None


def test_exposure_samples_join_actual_partial_fills_and_count_missing_allocations(
    monkeypatch,
):
    _, thesis, analytics = _assessed(monkeypatch)
    rows = journal.get_exit_decisions(thesis.position_key)
    rows[0]["payload"]["trace"]["mark_price"] = 108
    rows[0]["payload"]["trace"]["orchestration"]["actual_state_before"][
        "known_quantity"
    ] = 5
    monkeypatch.setattr(
        journal,
        "get_position_fills",
        lambda *a, **k: [
            {
                "side": "BUY",
                "quantity": 10,
                "fill_price": 100,
                "exchange_time": START.isoformat(),
            },
            {
                "side": "SELL",
                "quantity": 5,
                "fill_price": 104,
                "exchange_time": (START + timedelta(minutes=1)).isoformat(),
            },
        ],
    )
    managed = journal.get_managed_position(thesis.position_key)
    path = analytics._retained_exposure_path(journal, managed, thesis.to_dict(), rows)
    assert path[0]["realized_gross"] == 20
    assert path[0]["residual_quantity"] == 5
    monkeypatch.setattr(journal, "get_position_fills", lambda *a, **k: [])
    assert analytics._retained_exposure_path(
        journal, managed, thesis.to_dict(), rows
    ) == [{}]


def test_real_ledger_allocated_manual_reductions_restore_exposure_and_excursions(
    monkeypatch,
):
    import backend.trading_engine as module

    engine, thesis = _managed_engine()
    key = thesis.position_key
    journal.create_order_intent(
        intent_id="owned-entry",
        position_key=key,
        trade_id=thesis.trade_id,
        intent_type="ENTER",
        role="ENTRY",
        side="BUY",
        quantity=10,
    )
    journal.prepare_order_attempt(
        intent_id="owned-entry", attempt_id="entry-attempt", attempt_tag="entry-tag"
    )
    journal.record_order_attempt_state(
        "entry-attempt", "FILLED", broker_order_id="ENTRY"
    )
    journal.record_order_fill(
        broker_fill_id="entry-fill",
        broker_order_id="ENTRY",
        position_key=key,
        side="BUY",
        quantity=10,
        fill_price=100,
        exchange_time=START,
    )
    for order, at, price in (("MANUAL-PARTIAL", 1, 104), ("MANUAL-FINAL", 9, 106)):
        journal.record_order_fill(
            broker_fill_id=order,
            broker_order_id=order,
            position_key=key,
            side="SELL",
            quantity=5,
            fill_price=price,
            exchange_time=START + timedelta(minutes=at),
            raw_fill={"allocated_position_key": key},
        )
    # Neither unallocated symbol history nor another epoch/account belongs here.
    for index, other_key in enumerate(
        (key, key.replace("epoch-1", "epoch-2"), key.replace("acct-1", "acct-2"))
    ):
        journal.record_order_fill(
            broker_fill_id=f"unrelated-{index}",
            broker_order_id=f"unrelated-{index}",
            position_key=other_key,
            side="SELL",
            quantity=10,
            fill_price=1,
            exchange_time=START + timedelta(minutes=1),
            raw_fill={} if other_key == key else {"allocated_position_key": other_key},
        )
    assert (
        len(
            journal.get_position_fills(
                key, broker_order_ids={"ENTRY"}, include_allocated_external=True
            )
        )
        == 3
    )
    monkeypatch.setattr(module, "now_utc", lambda: START + timedelta(minutes=6))
    engine._record_shadow_quote_observation(
        key,
        trade=engine.active_trades["RELIANCE"],
        observations=[
            {"mark": 98, "observed_at": START + timedelta(seconds=30)},
            {"mark": 108, "observed_at": START + timedelta(minutes=5)},
        ],
    )
    analytics = TradeAnalytics(str(journal.db_path))
    managed = journal.get_managed_position(key)
    path = analytics._retained_exposure_path(journal, managed, thesis.to_dict(), [])
    assert [
        (point["residual_quantity"], point["realized_gross"]) for point in path
    ] == [(10, 0), (5, 20)]
    result = analytics._exit_quality_record(journal, {**_trade(thesis), "quantity": 10})
    assert result["eligible"]
    assert result["metrics"]["mfe_r"] == 1.6
    assert result["metrics"]["mae_r"] == 0.4
    assert result["metrics"]["exposure_peak_gross"] == 60
    assert result["metrics"]["exposure_aware_r_given_back"] == 0.2
