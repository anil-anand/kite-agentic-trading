"""Broker observations preserve historical risk and durable management state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.broker_models import OrderRole
from backend.exit_management.models import LifecycleEvent, reduce_lifecycle
from backend.order_lifecycle import IntentType
from backend.tests.test_review5_lifecycle import lifecycle as lifecycle
from backend.time_utils import now_utc


def _open_with_history(env):
    env.sdk.after_entry = lambda: env.sdk.fill_entry()
    assert env.engine.execute_signal(env.signal)
    trade = env.engine.active_trades["RELIANCE"]
    key = trade["exit_management_position_key"]
    state = env.engine._phase5_state_for(key)
    state = reduce_lifecycle(
        state,
        LifecycleEvent.STATE_OBSERVED,
        event_id="retained-observation-history",
        occurred_at=now_utc(),
    )
    env.engine._commit_phase5_checkpoint(
        position_key=key,
        state=state,
        event_id="retained-observation-history",
        event_type="STATE_OBSERVED",
        details={"source": "fixture"},
        counters={"eligible_completed_bars": 7, "weakening_bars": 1},
        extrema={"observed_mfe": 1.7, "observed_mae": 0.2},
        intents={"prior_observation_reference": "earlier-intent"},
        input_references={"continuation_context": "retained-source-prefix"},
    )
    return key, trade, env.journal.get_position_thesis(key)["payload"]


def _checkpoint(env, key):
    return env.journal.get_managed_position(key)["state"]


def _assert_history_retained(env, key, thesis):
    checkpoint = _checkpoint(env, key)
    assert checkpoint["counters"]["eligible_completed_bars"] == 7
    assert checkpoint["counters"]["weakening_bars"] == 1
    assert checkpoint["extrema"]["observed_mfe"] == 1.7
    assert checkpoint["extrema"]["observed_mae"] == 0.2
    assert checkpoint["intents"]["entry_intent_id"]
    assert checkpoint["intents"]["prior_observation_reference"] == "earlier-intent"
    assert checkpoint["input_references"]["continuation_context"] == (
        "retained-source-prefix"
    )
    assert env.journal.get_position_thesis(key)["payload"] == thesis
    assert thesis["initial_stop"] == 95
    assert thesis["fill_binding"]["initial_r_per_share"] == 6
    assert thesis["fill_binding"]["initial_risk_budget"] == 60


def test_confirmed_breakeven_checkpoint_preserves_original_r_and_history(
    lifecycle, monkeypatch
):
    e = lifecycle
    key, _, thesis = _open_with_history(e)

    def modify(**kwargs):
        e.sdk.book[1].update(kwargs)
        return "O2"

    monkeypatch.setattr(e.sdk, "modify_order", modify, raising=False)
    assert e.engine._tighten_to_breakeven("RELIANCE")
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop"] == 101
    assert checkpoint["protection"].get("requested_stop") is None
    assert checkpoint["state"]["protection"] == "ACTIVE"
    _assert_history_retained(e, key, thesis)


def test_lost_modify_ack_does_not_promote_requested_stop_until_broker_observes_it(
    lifecycle, monkeypatch
):
    e = lifecycle
    key, _, thesis = _open_with_history(e)
    modifications = []

    def accepted_unchanged(**kwargs):
        modifications.append(kwargs)
        return "O2"

    monkeypatch.setattr(e.sdk, "modify_order", accepted_unchanged, raising=False)
    assert e.engine._tighten_to_breakeven("RELIANCE") is False
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop"] == 95
    assert checkpoint["protection"]["requested_stop"] == 101
    assert checkpoint["state"]["protection"] == "UPDATE_PENDING"
    _assert_history_retained(e, key, thesis)

    e.sdk.book[1].update(trigger_price=101, price=99.99)
    e.engine._tighten_to_breakeven("RELIANCE")
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop"] == 101
    assert checkpoint["protection"].get("requested_stop") is None
    assert checkpoint["state"]["protection"] == "ACTIVE"
    assert len(modifications) == 1
    _assert_history_retained(e, key, thesis)


def test_stop_resize_checkpoint_uses_actual_residual_and_preserves_history(
    lifecycle,
):
    e = lifecycle
    key, trade, thesis = _open_with_history(e)
    e.sdk.position_rows[0].update(quantity=6, day_sell_quantity=4, sell_quantity=4)
    e.sdk.executions.append(
        {
            **e.sdk.executions[0],
            "trade_id": "EXTERNAL-PARTIAL",
            "order_id": "MANUAL",
            "transaction_type": "SELL",
            "quantity": 4,
            "average_price": 103,
        }
    )

    def cancel(variety, order_id, **kwargs):
        e.sdk.book[1].update(status="CANCELLED", pending_quantity=0)
        return order_id

    e.sdk.cancel_order = cancel
    assert e.engine._ensure_recovery_protection(
        "RELIANCE", e.engine._positions()[0], trade
    )
    checkpoint = _checkpoint(e, key)
    assert checkpoint["state"]["known_quantity"] == 6
    assert checkpoint["protection"]["protected_quantity"] == 6
    assert checkpoint["protection"]["confirmed_stop_order_id"] == "O3"
    assert e.sdk.calls[-1]["quantity"] == 6
    _assert_history_retained(e, key, thesis)


def test_concurrent_and_late_broker_observations_cannot_overwrite_hard_exit_latch(
    lifecycle,
):
    e = lifecycle
    key, trade, thesis = _open_with_history(e)
    stale_trade = dict(trade)
    stop = dict(e.engine._find_order("O2"), trigger_price=101)
    intent = e.engine._order_lifecycle.prepare_intent(
        position_key=key,
        intent_type=IntentType.FLATTEN,
        role=OrderRole.REDUCTION,
        side="SELL",
        quantity=10,
        payload={"recovery_trade": dict(trade)},
        trade_id=trade["trade_id"],
        reason="Stop Loss",
        latched=True,
    )
    start = Barrier(2)

    def observe():
        start.wait(timeout=5)
        e.engine._record_phase5_broker_observation(
            key, trade=stale_trade, stop=stop, residual_quantity=10
        )

    def hard_exit():
        start.wait(timeout=5)
        e.engine._record_phase5_exit_pending(
            key, intent_id=intent["intent_id"], reason="Stop Loss", quantity=10
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        observed = workers.submit(observe)
        exiting = workers.submit(hard_exit)
        observed.result(timeout=5)
        exiting.result(timeout=5)

    # A network response started before the hard exit can also arrive later.
    e.engine._record_phase5_broker_observation(
        key, trade=stale_trade, stop=stop, residual_quantity=10
    )
    checkpoint = _checkpoint(e, key)
    assert checkpoint["state"]["exposure"] == "EXIT_PENDING"
    assert checkpoint["state"]["latched_exit_intent_id"] == intent["intent_id"]
    assert checkpoint["intents"]["exit_intent_id"] == intent["intent_id"]
    versions = [row["state_version"] for row in e.journal.get_position_checkpoints(key)]
    assert versions == sorted(set(versions))
    _assert_history_retained(e, key, thesis)


@pytest.mark.parametrize("failure", ["checkpoint", "invalid_fill_geometry"])
def test_partial_fill_bookkeeping_failure_cannot_skip_immediate_protection(
    lifecycle, monkeypatch, failure
):
    e = lifecycle

    def partial_fill():
        e.sdk.fill_entry(4, "OPEN")
        if failure == "invalid_fill_geometry":
            # A favorable gap may fill a BUY below its planned stop. Recording
            # a signed R then fails, but known exposure still needs protection.
            e.sdk.position_rows[0]["average_price"] = 94
            e.sdk.executions[0]["average_price"] = 94

    e.sdk.after_entry = partial_fill
    if failure == "checkpoint":
        commit = e.journal.commit_position_checkpoint

        def fail_partial(checkpoint, **kwargs):
            if kwargs.get("event_type") == "ENTRY_PARTIAL_FILL_OBSERVED":
                raise OSError("injected partial-fill checkpoint failure")
            return commit(checkpoint, **kwargs)

        monkeypatch.setattr(e.journal, "commit_position_checkpoint", fail_partial)

    assert e.engine.execute_signal(e.signal) is False
    trade = e.engine.active_trades["RELIANCE"]
    assert trade["entry_state"] == "RECOVERY_REQUIRED"
    assert trade["stop_order_id"] == "O2"
    assert e.sdk.calls[-1]["order_type"] == "SL"
    assert e.sdk.calls[-1]["quantity"] == 4
    assert len(e.sdk.calls) == 2


def test_late_older_stop_trigger_cannot_roll_back_confirmed_tightening(
    lifecycle, monkeypatch
):
    e = lifecycle
    key, trade, thesis = _open_with_history(e)
    stale_trade = dict(trade)
    stale_stop = dict(e.engine._find_order("O2"))

    def modify(**kwargs):
        e.sdk.book[1].update(kwargs)
        return "O2"

    monkeypatch.setattr(e.sdk, "modify_order", modify, raising=False)
    assert e.engine._tighten_to_breakeven("RELIANCE")
    assert _checkpoint(e, key)["protection"]["confirmed_stop"] == 101

    e.engine._record_phase5_broker_observation(
        key, trade=stale_trade, stop=stale_stop, residual_quantity=10
    )
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop"] == 101
    assert checkpoint["protection"].get("requested_stop") is None
    assert checkpoint["state"]["protection"] == "ACTIVE"
    assert e.engine.active_trades["RELIANCE"]["sl"] == 101
    _assert_history_retained(e, key, thesis)


@pytest.mark.parametrize("stale_owner", [True, False])
def test_late_predecessor_stop_cannot_replace_confirmed_successor_links(
    lifecycle, stale_owner
):
    e = lifecycle
    key, trade, thesis = _open_with_history(e)
    old_trade = dict(trade)
    old_stop = dict(e.engine._find_order("O2"))
    e.sdk.book[1].update(status="CANCELLED", pending_quantity=0)
    assert e.engine._ensure_recovery_protection(
        "RELIANCE", e.engine._positions()[0], trade
    )
    current = e.engine.active_trades["RELIANCE"]
    assert current["stop_order_id"] == "O3"
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop_order_id"] == "O3"

    e.engine._record_phase5_broker_observation(
        key,
        trade=old_trade if stale_owner else dict(current),
        stop=old_stop,
        residual_quantity=10,
    )
    checkpoint = _checkpoint(e, key)
    assert checkpoint["protection"]["confirmed_stop_order_id"] == "O3"
    assert checkpoint["intents"]["stop_order_id"] == "O3"
    assert (
        checkpoint["intents"]["protection_intent_id"]
        == (current["protection_intent_id"])
    )
    assert checkpoint["state"]["protection"] == "ACTIVE"
    _assert_history_retained(e, key, thesis)


@pytest.mark.parametrize("event", ["partial_fill", "entry_recovery"])
def test_duplicate_partial_entry_event_after_other_observations_is_a_noop(
    lifecycle, event
):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(4, "OPEN")
    assert e.engine.execute_signal(e.signal) is False
    trade = e.engine.active_trades["RELIANCE"]
    key = trade["exit_management_position_key"]
    assert not e.engine._reconciliation_pending
    assert not trade.get("exit_state_recovery_required")

    # A later protection/counter observation must not erase deduplication of
    # an earlier fill event. Broker polling commonly repeats that same fill.
    observed_trade = dict(trade, last_reeval_time=now_utc())
    e.engine._record_phase5_broker_observation(
        key,
        trade=observed_trade,
        stop=e.engine._find_order("O2"),
        residual_quantity=4,
    )
    before = _checkpoint(e, key)
    sequence = e.journal.get_managed_position(key)["checkpoint_sequence"]
    if event == "partial_fill":
        e.engine._record_phase5_provisional_risk(
            key, fill_price=101, quantity=4, event_id="entry-partial:O1:4"
        )
    else:
        e.engine._record_phase5_entry_recovery(
            key,
            event_id="entry-partial-recovery:O1:4",
            detail="entry remainder remains working after a partial fill",
        )
    assert _checkpoint(e, key) == before
    assert e.journal.get_managed_position(key)["checkpoint_sequence"] == sequence
    assert not e.engine._reconciliation_pending
    assert not trade.get("exit_state_recovery_required")
    assert len(e.sdk.calls) == 2


def test_provisional_entry_risk_does_not_replace_smaller_verified_residual(lifecycle):
    e = lifecycle
    e.sdk.after_entry = lambda: e.sdk.fill_entry(4, "OPEN")
    assert e.engine.execute_signal(e.signal) is False
    key = e.engine.active_trades["RELIANCE"]["exit_management_position_key"]
    # Six cumulative entry shares and two stop fills leave four live shares.
    e.engine._record_phase5_provisional_risk(
        key,
        fill_price=101,
        quantity=6,
        residual_quantity=4,
        event_id="entry-partial:O1:6",
    )
    checkpoint = _checkpoint(e, key)
    assert checkpoint["state"]["known_quantity"] == 4
    assert checkpoint["protection"]["protected_quantity"] == 4
    assert checkpoint["counters"]["provisional_risk"]["filled_quantity"] == 6
    assert checkpoint["counters"]["provisional_risk"]["risk_budget"] == 36
