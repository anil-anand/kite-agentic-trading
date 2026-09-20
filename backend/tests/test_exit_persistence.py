import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from backend.exit_management.models import (
    DecisionRecord,
    LifecycleEvent,
    PositionCheckpoint,
    PositionState,
    ProtectionState,
    ThesisHealth,
    reduce_lifecycle,
)
from backend.exit_management.thesis import bind_terminal_fill, capture_entry_thesis
from backend.journal import TradeJournal


def _signal(symbol="RELIANCE"):
    return {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "product": "MIS",
        "direction": "BUY",
        "strategy": "Breakout",
        "playbook": "Breakout",
        "setup_variant": "breakout_trigger",
        "entryPrice": 100.0,
        "stopLoss": 95.0,
        "target": 110.0,
        "selected_evidence": [
            {
                "strategy_id": "donchian_breakout",
                "family": "breakout",
                "direction": "BUY",
                "signal_score": 80,
            }
        ],
        "market_context": {
            "policy": {"policyVersion": "market-context-v1"},
            "primaryBar": {"end": "2026-09-20T04:25:00+00:00"},
        },
    }


def _config():
    return {"exitManagement": {"policyVersion": "exit-thesis-state-v1"}}


def _draft(account="acct-1", symbol="RELIANCE", epoch="epoch-1"):
    key = f"LIVE:{account}:NSE:1:{symbol}:MIS:{epoch}"
    return capture_entry_thesis(
        _signal(symbol),
        position_key=key,
        trade_id=f"trade-{account}-{epoch}",
        position_epoch=epoch,
        instrument_id="1",
        effective_config=_config(),
        created_at="2026-09-20T04:25:01+00:00",
    )


def _pending_checkpoint(thesis, intent_id="entry-intent-1"):
    state = reduce_lifecycle(
        PositionState(
            position_key=thesis.position_key,
            thesis_health=ThesisHealth.VALID,
        ),
        LifecycleEvent.ENTRY_INTENT_COMMITTED,
        event_id=f"entry-intent:{intent_id}",
        occurred_at="2026-09-20T04:25:01+00:00",
    )
    return state, PositionCheckpoint(
        position_key=thesis.position_key,
        state_version=state.version,
        sequence=0,
        state=state,
        intents={"entry_intent_id": intent_id},
        protection={"requested_initial_stop": thesis.initial_stop},
    )


def _entry_intent(thesis, intent_id="entry-intent-1"):
    return {
        "intent_id": intent_id,
        "position_key": thesis.position_key,
        "trade_id": thesis.trade_id,
        "intent_type": "ENTER",
        "role": "ENTRY",
        "side": "BUY",
        "quantity": 10,
        "payload": {"thesis_id": thesis.thesis_id},
    }


def test_thesis_entry_intent_and_checkpoint_commit_as_one_transaction(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, checkpoint = _pending_checkpoint(thesis)

    stored = journal.create_managed_position(
        thesis, checkpoint, entry_intent=_entry_intent(thesis)
    )
    assert stored["state"]["state"]["exposure"] == "ENTRY_PENDING"
    assert (
        journal.get_position_thesis(thesis.position_key)["payload"]["thesis_id"]
        == thesis.thesis_id
    )
    assert journal.get_order_intent("entry-intent-1")["state"] == "PREPARED"
    assert journal.get_position_checkpoints(thesis.position_key)[0]["sequence"] == 0

    # An invalid intent rolls the whole registration back: no orphan thesis or
    # checkpoint can survive the failed transaction.
    rejected = _draft(epoch="epoch-bad")
    _, rejected_checkpoint = _pending_checkpoint(rejected, "entry-intent-bad")
    invalid_intent = _entry_intent(rejected, "entry-intent-bad")
    invalid_intent["quantity"] = 0
    with pytest.raises(ValueError, match="quantity"):
        journal.create_managed_position(
            rejected, rejected_checkpoint, entry_intent=invalid_intent
        )
    assert journal.get_managed_position(rejected.position_key) is None
    assert journal.get_position_thesis(rejected.position_key) is None


def test_fill_binding_and_ordered_checkpoint_replay_are_immutable(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, checkpoint = _pending_checkpoint(thesis)
    journal.create_managed_position(
        thesis, checkpoint, entry_intent=_entry_intent(thesis)
    )

    bound = bind_terminal_fill(
        thesis,
        entry_vwap=101.0,
        filled_quantity=10,
        terminal_at="2026-09-20T04:26:00+00:00",
        source_fill_ids=("fill-1",),
    )
    opened = reduce_lifecycle(
        state,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        event_id="entry-open-1",
        occurred_at="2026-09-20T04:26:00+00:00",
        protection=ProtectionState.ACTIVE,
        known_quantity=10,
    )
    next_checkpoint = PositionCheckpoint(
        position_key=thesis.position_key,
        state_version=opened.version,
        sequence=1,
        state=opened,
        protection={"confirmed_stop": 95.0, "protected_quantity": 10},
    )
    decision = DecisionRecord(
        decision_id="decision-entry-open-1",
        position_key=thesis.position_key,
        occurred_at="2026-09-20T04:26:00+00:00",
        action="HOLD",
        primary_reason_code="HOLD_THESIS_VALID",
        policy_version=bound.policy_snapshot.policy_version,
        state_before=state,
        state_after=opened,
        input_references={"thesis_id": bound.thesis_id},
    )
    journal.commit_position_checkpoint(
        next_checkpoint,
        event_id="entry-open-1",
        event_type="ENTRY_TERMINAL_PROTECTED",
        decision=decision,
        bound_thesis=bound,
    )

    assert (
        journal.get_position_thesis(thesis.position_key, revision=1)["payload"][
            "fill_binding"
        ]
        is None
    )
    assert (
        journal.get_position_thesis(thesis.position_key)["payload"]["fill_binding"][
            "initial_risk_budget"
        ]
        == 60.0
    )
    assert [
        item["sequence"]
        for item in journal.get_position_checkpoints(thesis.position_key)
    ] == [0, 1]
    assert (
        journal.get_exit_decisions(thesis.position_key)[0]["payload"]["action"]
        == "HOLD"
    )

    stale = PositionCheckpoint(
        position_key=thesis.position_key,
        state_version=opened.version + 1,
        sequence=2,
        state=reduce_lifecycle(
            opened,
            LifecycleEvent.ENTRY_UPDATE,
            event_id="state-2",
            occurred_at="2026-09-20T04:30:00+00:00",
        ),
    )
    with pytest.raises(RuntimeError, match="stale"):
        journal.commit_position_checkpoint(
            stale,
            event_id="state-2",
            event_type="ENTRY_UPDATE",
            expected_state_version=0,
        )


def test_legacy_import_preserves_unknowns_and_corrupted_records_remain_visible(
    tmp_path,
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    imported = journal.import_legacy_active_snapshot(
        {
            "RELIANCE": {
                "sl": 95.0,
                "target": 110.0,
                # No canonical identity or verified quantity: importing must
                # not invent either one.
                "quantity": 10,
            }
        }
    )
    assert len(imported) == 1
    record = journal.get_managed_position(imported[0])
    assert record["provenance"] == "LEGACY_PARTIAL"
    assert record["state"]["state"]["known_quantity"] is None
    assert record["state"]["protection"]["legacy_target"] == 110.0
    assert (
        journal.import_legacy_active_snapshot(
            {"RELIANCE": {"sl": 95.0, "target": 110.0, "quantity": 10}}
        )
        == imported
    )

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    conn.execute(
        "UPDATE managed_positions SET current_state = ? WHERE position_key = ?",
        ("{broken", imported[0]),
    )
    conn.commit()
    conn.close()
    corrupt = journal.get_managed_position(imported[0])
    assert corrupt["state"] is None
    assert corrupt["state_corrupt"] is True


def test_position_namespace_prevents_same_symbol_cross_account_collision(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    first = _draft(account="acct-1")
    second = _draft(account="acct-2")
    for thesis, intent_id in ((first, "entry-1"), (second, "entry-2")):
        _, checkpoint = _pending_checkpoint(thesis, intent_id)
        journal.create_managed_position(
            thesis, checkpoint, entry_intent=_entry_intent(thesis, intent_id)
        )
    assert journal.get_managed_position(first.position_key)["account_id"] == "acct-1"
    assert journal.get_managed_position(second.position_key)["account_id"] == "acct-2"


def test_engine_registers_thesis_and_entry_intent_before_broker_submission(
    monkeypatch, tmp_path
):
    import backend.trading_engine as engine_module

    journal = TradeJournal(str(tmp_path / "journal.db"))
    monkeypatch.setattr(engine_module, "journal", journal)
    engine = engine_module.TradingEngine()
    signal = _signal()
    key = "LIVE:acct-1:NSE:1:RELIANCE:MIS:epoch-engine"

    thesis, intent_id = engine._register_entry_thesis(
        signal=signal,
        position_key=key,
        trade_id="trade-engine",
        position_epoch="epoch-engine",
        instrument_id="1",
        quantity=10,
        transaction_type="BUY",
        order_kwargs={
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "product": "MIS",
            "quantity": 10,
        },
        recovery_trade={
            "tradingsymbol": "RELIANCE",
            "sl": 95.0,
            "target": 110.0,
            "direction": "BUY",
            "quantity": 10,
        },
    )

    assert journal.get_order_intent(intent_id)["state"] == "PREPARED"
    assert journal.get_managed_position(key)["thesis_id"] == thesis.thesis_id
    assert (
        journal.get_position_checkpoints(key)[0]["payload"]["state"]["exposure"]
        == "ENTRY_PENDING"
    )
    engine._record_phase5_exit_pending(
        key,
        intent_id="exit-intent-engine",
        reason="legacy_control_exit",
        quantity=10,
    )
    engine._record_phase5_flat_observed(key, event_id="flat-engine")
    engine._record_phase5_closed(key, event_id="closed-engine")
    assert journal.get_managed_position(key)["state"]["state"]["exposure"] == "CLOSED"


def test_engine_restart_restores_checkpoint_without_rebuilding_thesis(
    monkeypatch, tmp_path
):
    import backend.trading_engine as engine_module

    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft(epoch="epoch-restart")
    _, checkpoint = _pending_checkpoint(thesis, "restart-entry")
    journal.create_managed_position(
        thesis, checkpoint, entry_intent=_entry_intent(thesis, "restart-entry")
    )
    monkeypatch.setattr(engine_module, "journal", journal)
    engine = engine_module.TradingEngine()
    engine.active_trades = {
        "RELIANCE": {
            "exit_management_position_key": thesis.position_key,
            "sl": 95.0,
            "target": 110.0,
        }
    }

    engine._restore_phase5_checkpoints()

    restored = engine.active_trades["RELIANCE"]
    assert restored["exit_management_checkpoint_version"] == 1
    assert (
        restored["exit_management_checkpoint"]["intents"]["entry_intent_id"]
        == "restart-entry"
    )
    assert (
        engine._managed_position_states[thesis.position_key].exposure.value
        == "ENTRY_PENDING"
    )


def _terminal_transition(thesis, state, *, event_id="terminal-fill"):
    bound = bind_terminal_fill(
        thesis,
        entry_vwap=101.0,
        filled_quantity=10,
        terminal_at="2026-09-20T04:26:00+00:00",
        source_fill_ids=("fill-1",),
    )
    opened = reduce_lifecycle(
        state,
        LifecycleEvent.ENTRY_TERMINAL_PROTECTED,
        event_id=event_id,
        occurred_at="2026-09-20T04:26:00+00:00",
        protection=ProtectionState.ACTIVE,
        known_quantity=10,
    )
    checkpoint = PositionCheckpoint(
        position_key=thesis.position_key,
        state_version=opened.version,
        sequence=1,
        state=opened,
        counters={"episode": {"count": 2, "bar_ids": ["bar-1", "bar-2"]}},
        extrema={"mfe_r": 1.25, "mae_r": 0.5},
        intents={"entry_intent_id": "entry-intent-1"},
        protection={"confirmed_stop": 95.0, "protected_quantity": 10},
    )
    return bound, checkpoint


def test_binding_checkpoint_and_trace_roll_back_after_sql_failure(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    bound, opened = _terminal_transition(thesis, state)
    decision = DecisionRecord(
        decision_id="terminal-decision",
        position_key=thesis.position_key,
        occurred_at="2026-09-20T04:26:00+00:00",
        action="HOLD",
        primary_reason_code="HOLD_THESIS_VALID",
        policy_version=thesis.policy_snapshot.policy_version,
        state_before=state,
        state_after=opened.state,
        input_references={"entry": {"thesis_id": thesis.thesis_id}},
    )
    conn = journal._get_conn()
    conn.execute(
        "CREATE TEMP TRIGGER fail_trace BEFORE INSERT ON exit_decision_records "
        "BEGIN SELECT RAISE(ABORT, 'injected crash boundary'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
        journal.commit_position_checkpoint(
            opened,
            event_id="terminal-fill",
            event_type="ENTRY_TERMINAL_PROTECTED",
            decision=decision,
            bound_thesis=bound,
        )
    assert journal.get_position_thesis(thesis.position_key)["revision"] == 1
    assert journal.get_managed_position(thesis.position_key)["state_version"] == 1
    assert len(journal.get_position_checkpoints(thesis.position_key)) == 1
    assert journal.get_exit_decisions(thesis.position_key) == []
    assert (
        conn.execute("SELECT COUNT(*) FROM position_lifecycle_events").fetchone()[0]
        == 1
    )

    conn.execute("DROP TRIGGER fail_trace")
    journal.commit_position_checkpoint(
        opened,
        event_id="terminal-fill",
        event_type="ENTRY_TERMINAL_PROTECTED",
        decision=decision,
        bound_thesis=bound,
    )
    assert journal.get_position_thesis(thesis.position_key)["revision"] == 2
    assert (
        journal.get_managed_position(thesis.position_key)["state"]["extrema"]["mfe_r"]
        == 1.25
    )
    encoded = conn.execute(
        "SELECT input_reference FROM exit_decision_records"
    ).fetchone()[0]
    assert json.loads(encoded) == {"entry": {"thesis_id": thesis.thesis_id}}


def test_duplicate_registration_cannot_change_entry_premise_or_intent(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    _, checkpoint = _pending_checkpoint(thesis)
    journal.create_managed_position(
        thesis, checkpoint, entry_intent=_entry_intent(thesis)
    )
    with pytest.raises(ValueError, match="conflicting duplicate"):
        journal.create_managed_position(replace(thesis, initial_stop=94.0), checkpoint)
    changed_intent = _entry_intent(thesis)
    changed_intent["payload"] = {"thesis_id": "a-different-premise"}
    with pytest.raises(ValueError, match="conflicting duplicate"):
        journal.create_managed_position(thesis, checkpoint, entry_intent=changed_intent)
    assert (
        journal.get_position_thesis(thesis.position_key)["payload"]["initial_stop"]
        == 95.0
    )


def test_fill_binding_cannot_change_entry_premise(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    bound, checkpoint = _terminal_transition(replace(thesis, initial_stop=94.0), state)
    with pytest.raises(ValueError, match="cannot change the entry premise"):
        journal.commit_position_checkpoint(
            checkpoint,
            event_id="terminal-fill",
            event_type="ENTRY_TERMINAL_PROTECTED",
            bound_thesis=bound,
        )
    assert journal.get_position_thesis(thesis.position_key)["revision"] == 1
    assert journal.get_managed_position(thesis.position_key)["state_version"] == 1


def test_checkpoint_event_idempotency_rejects_conflicting_retries(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    bound, checkpoint = _terminal_transition(thesis, state)
    for _ in range(2):
        journal.commit_position_checkpoint(
            checkpoint,
            event_id="terminal-fill",
            event_type="ENTRY_TERMINAL_PROTECTED",
            bound_thesis=bound,
        )
    with pytest.raises(ValueError, match="conflicting duplicate checkpoint"):
        journal.commit_position_checkpoint(
            replace(checkpoint, counters={"episode": {"count": 200}}),
            event_id="terminal-fill",
            event_type="ENTRY_TERMINAL_PROTECTED",
        )
    assert len(journal.get_position_checkpoints(thesis.position_key)) == 2
    assert (
        journal.get_managed_position(thesis.position_key)["state"]["counters"][
            "episode"
        ]["count"]
        == 2
    )


def test_trace_cannot_describe_a_different_transition(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    bound, checkpoint = _terminal_transition(thesis, state)
    mismatched = DecisionRecord(
        decision_id="different-state",
        position_key=thesis.position_key,
        occurred_at="2026-09-20T04:26:00+00:00",
        action="HOLD",
        primary_reason_code="HOLD_THESIS_VALID",
        policy_version=thesis.policy_snapshot.policy_version,
        state_before=replace(state, thesis_health=ThesisHealth.UNKNOWN),
        state_after=checkpoint.state,
    )
    with pytest.raises(ValueError, match="committed state transition"):
        journal.commit_position_checkpoint(
            checkpoint,
            event_id="terminal-fill",
            event_type="ENTRY_TERMINAL_PROTECTED",
            bound_thesis=bound,
            decision=mismatched,
        )
    assert journal.get_position_thesis(thesis.position_key)["revision"] == 1


def test_concurrent_checkpoint_writers_cannot_overwrite_winning_state(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    barrier = Barrier(2)

    def advance(event_id):
        changed = reduce_lifecycle(
            state,
            LifecycleEvent.ENTRY_UPDATE,
            event_id=event_id,
            occurred_at="2026-09-20T04:26:00+00:00",
            known_quantity=3,
        )
        checkpoint = PositionCheckpoint(
            position_key=thesis.position_key,
            state_version=changed.version,
            sequence=1,
            state=changed,
            counters={"winning_event": event_id},
        )
        barrier.wait(timeout=5)
        try:
            journal.commit_position_checkpoint(
                checkpoint,
                event_id=event_id,
                event_type="ENTRY_UPDATE",
                expected_state_version=state.version,
            )
            return event_id
        except RuntimeError as exc:
            assert "stale" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, ["concurrent-a", "concurrent-b"]))
    winners = [result for result in results if result]
    assert len(winners) == 1
    assert journal.get_managed_position(thesis.position_key)["state"]["counters"] == {
        "winning_event": winners[0]
    }
    assert len(journal.get_position_checkpoints(thesis.position_key)) == 2


@pytest.mark.parametrize(
    "damage", ["state_identity", "state_version", "checkpoint", "thesis"]
)
def test_valid_json_corruption_is_detected_and_remains_visible(tmp_path, damage):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    _, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    conn = journal._get_conn()
    if damage == "thesis":
        payload = thesis.to_dict()
        payload["initial_stop"] = 50.0
        conn.execute("UPDATE position_theses SET payload = ?", (json.dumps(payload),))
        assert journal.get_position_thesis(thesis.position_key)["payload_corrupt"]
    else:
        payload = pending.to_dict()
        table, field = "managed_positions", "current_state"
        if damage == "state_identity":
            payload["state"]["position_key"] = (
                "PAPER:another-account:NSE:1:RELIANCE:MIS:e"
            )
        elif damage == "state_version":
            payload["state"]["version"] = 999
        else:
            table, field = "position_checkpoints", "payload"
            payload["counters"] = {"changed": True}
        conn.execute(f"UPDATE {table} SET {field} = ?", (json.dumps(payload),))
        assert journal.get_managed_position(thesis.position_key)["state_corrupt"]
        assert journal.list_managed_positions(namespace="LIVE", account_id="acct-1")[0][
            "state_corrupt"
        ]
    assert journal.list_managed_positions(namespace="PAPER", account_id="acct-1") == []
    assert (
        journal.list_managed_positions(namespace="LIVE", account_id="different") == []
    )


def test_nonfinite_checkpoint_cannot_enter_durable_state(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    _, pending = _pending_checkpoint(thesis)
    with pytest.raises(ValueError):
        journal.create_managed_position(
            thesis,
            replace(pending, extrema={"mfe_r": float("nan")}),
            entry_intent=_entry_intent(thesis),
        )
    assert journal.get_managed_position(thesis.position_key) is None


def test_changed_legacy_snapshot_keeps_trade_identity_and_each_original_backup(
    tmp_path,
):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    original = {"RELIANCE": {"trade_id": "legacy-1", "sl": 95.0, "target": 110.0}}
    first = journal.import_legacy_active_snapshot(original)
    updated = {"RELIANCE": {"trade_id": "legacy-1", "sl": 97.0, "target": 110.0}}
    assert journal.import_legacy_active_snapshot(updated) == first
    conn = journal._get_conn()
    assert (
        conn.execute("SELECT COUNT(*) FROM legacy_position_snapshots").fetchone()[0]
        == 2
    )
    assert conn.execute("SELECT COUNT(*) FROM position_key_aliases").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM managed_positions").fetchone()[0] == 1
    stored = journal.get_managed_position(first[0])
    assert stored["state"]["protection"]["legacy_stop_loss"] == 95.0
    assert stored["state"]["state"]["known_quantity"] is None


def test_older_draft_binds_without_inventing_new_profile_or_evidence(tmp_path):
    from backend.exit_management.thesis import EntryThesis

    journal = TradeJournal(str(tmp_path / "journal.db"))
    thesis = _draft()
    state, pending = _pending_checkpoint(thesis)
    journal.create_managed_position(thesis, pending, entry_intent=_entry_intent(thesis))
    older_payload = thesis.to_dict()
    older_payload.pop("management_profile")
    older_payload.pop("selection_inputs")
    journal._get_conn().execute(
        "UPDATE position_theses SET payload = ?, payload_hash = ?",
        (
            journal._exit_payload(older_payload),
            journal._exit_payload_hash(older_payload),
        ),
    )
    restored = EntryThesis.from_dict(
        journal.get_position_thesis(thesis.position_key)["payload"]
    )
    bound, opened = _terminal_transition(restored, state)
    journal.commit_position_checkpoint(
        opened,
        event_id="terminal-fill",
        event_type="ENTRY_TERMINAL_PROTECTED",
        bound_thesis=bound,
    )
    stored = journal.get_position_thesis(thesis.position_key)["payload"]
    assert stored["management_profile"]["name"] == "unknown_legacy_bounded"
    assert stored["selection_inputs"] == []
    assert stored["initial_stop"] == older_payload["initial_stop"]
