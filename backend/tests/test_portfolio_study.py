"""Portfolio state changes, rather than a fixed opportunity list, own admission."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from backend.backtesting import portfolio_study as study
from backend.backtesting.research_study import run_paired_case
from backend.backtesting.simulated_broker import (
    SimulatedBroker,
    SimulationExecutionPolicy,
)
from backend.exit_management.engine import ExitPolicy
from backend.tests.test_research_study import DAY, END, case, data

START = datetime.fromisoformat("2026-01-05T09:15:00+05:30")


def frames(symbols=("TEST",), *, volume=1000, stop_at=None):
    rows = []
    for index in (*range(8), 72, 74):
        price = 97 if index == stop_at else 100
        rows.append(
            dict(
                date=START + timedelta(minutes=5 * index),
                open=price,
                high=price + 0.2,
                low=price - 0.2,
                close=price,
                volume=volume,
            )
        )
    return {s: pd.DataFrame(rows) for s in symbols}


def config(**kw):
    return dict(
        maxDailyLoss=2000,
        noNewTradesAfter="15:00",
        startTradeAfter="09:15",
        maxCapitalPerTrade=1000,
        leverageMultiplier=1,
        riskPerTrade=20,
        maxSimultaneousPositions=1,
        maxTradesPerSymbolPerDay=10,
        tradeCooldownMins=0,
        **kw,
    )


def run(data=None, **kw):
    candles = data if data is not None else frames()
    values = dict(
        market_data=candles,
        universe_events=[{"at": START, "symbols": list(candles)}],
        instrument_metadata={
            s: {"instrument_id": f"token-{s}", "sector": "TEST"} for s in candles
        },
        strategy_config={},
        risk_config=config(),
        candidate_policy=ExitPolicy(),
        test_start=START,
        test_end=START + timedelta(hours=7),
        source_revision="test",
        execution_policy=SimulationExecutionPolicy(slippage_bps=0),
    )
    values.update(kw)
    return study.run_portfolio_study(**values)


@pytest.fixture
def signals(monkeypatch):
    def evaluate(**kw):
        if kw["decision_at"] >= START + timedelta(minutes=30):
            return []
        return [
            {
                "tradingsymbol": kw["symbol"],
                "direction": "BUY",
                "entryPrice": 100,
                "stopLoss": 98,
                "target": 110,
                "signal_score": 80,
                "strategy": "Trend Pullback",
                "playbook": "Trend Pullback",
                "market_context": kw["market_context"].summary(),
            }
        ]

    monkeypatch.setattr(study, "evaluate_production_entries", evaluate)


def test_production_risk_rejects_overlap_and_releases_epoch_capacity(signals):
    result = run(frames(stop_at=2))
    for branch in (result["candidate"], result["control"]):
        accepted = [r for r in branch["admissions"] if r["accepted"]]
        assert len(accepted) == 2
        assert len({r["position_key"] for r in accepted}) == 2
        assert any(
            r["reason"] == "DUPLICATE_POSITION_OR_ENTRY"
            for r in branch["rejected_opportunities"]
        )
        assert len(branch["trades"]) == 2
        assert branch["trades"][0]["exit_reason"] == "stop_loss"
        assert branch["parity"]["mismatches"] == 0
        assert branch["status"] == "COMPLETE"
        assert branch["entry_checkpoints"]


def test_pending_and_concurrent_symbols_consume_capacity(signals):
    result = run(frames(("B", "A")))
    records = result["candidate"]["admissions"]
    accepted = [r for r in records if r["accepted"]]
    assert accepted[0]["symbol"] == "B"  # retained screener rank
    assert len(accepted) == 1
    assert any("MAX_POSITIONS_LIMIT" in r["reason"] for r in records)


def test_partial_entry_is_terminal_and_protected_no_invented_full_fill(signals):
    result = run(
        execution_policy=SimulationExecutionPolicy(
            slippage_bps=0, max_fill_fraction=0.5
        )
    )
    branch = result["candidate"]
    entry = [o for o in branch["orders"] if o["role"] == "ENTRY"][0]
    assert entry["quantity"] == 10
    assert entry["filled_quantity"] == 5
    assert entry["status"] == "CANCELLED"
    assert branch["execution_coverage"]["scope"] == "HELD_EXPOSURE"
    assert branch["execution_coverage"]["entry_quantity_filled"] == 5
    assert (
        branch["entry_checkpoints"][0]["positions"][0]["thesis"]["fill_binding"][
            "filled_quantity"
        ]
        == 5
    )
    assert branch["censored_positions"]  # partial forced reduction lacks later bars


@pytest.mark.parametrize("latency", [1, 2])
def test_entry_timeout_during_latency_is_admission_only_coverage(signals, latency):
    result = run(
        execution_policy=SimulationExecutionPolicy(
            slippage_bps=0, order_latency_bars=latency
        )
    )
    for name in ("candidate", "control"):
        branch = result[name]
        coverage = branch["execution_coverage"]
        assert coverage["scope"] == "ENTRY_ADMISSION_ONLY"
        assert coverage["entry_orders_submitted"] > 0
        assert coverage["entry_orders_with_fills"] == 0
        assert coverage["entry_quantity_filled"] == 0
        assert coverage["completed_trades"] == 0
        assert branch["status"] == "COMPLETE"
        assert all(order["status"] == "CANCELLED" for order in branch["orders"])
        assert branch["manifest"]["entry_execution"].endswith("ONE_OBSERVED_BAR_TTL")


def test_missing_liquidity_never_becomes_a_fill(signals):
    result = run(frames(volume=0))
    assert not result["candidate"]["fills"]
    assert not result["candidate"]["trades"]


def test_universe_availability_and_future_input_are_enforced(signals):
    result = run(
        frames(("A", "B")),
        universe_events=[
            {"at": START, "symbols": ["A"]},
            {
                "at": START + timedelta(minutes=5),
                "available_at": START + timedelta(minutes=20),
                "symbols": ["B"],
            },
        ],
    )
    records = result["candidate"]["admissions"]
    assert all(
        r["symbol"] == "A"
        for r in records
        if datetime.fromisoformat(r["at"]) < START + timedelta(minutes=20)
    )
    with pytest.raises(ValueError, match="outside|cutoff"):
        run(test_end=START + timedelta(hours=1))


def test_zero_trade_sessions_are_marked_and_live_imports_absent():
    result = run()
    assert result["candidate"]["trades"] == []
    assert len(result["candidate"]["equity_curve"]) >= 10
    assert result["candidate"]["equity_curve"][-1]["equity"] == 100000


def test_session_snapshot_preserves_closed_turnover_and_excludes_previous_day():
    broker = SimulatedBroker(execution_policy=SimulationExecutionPolicy(slippage_bps=0))
    broker.place_market_order("TEST", "BUY", 10, 100, START, {"stopLoss": 98})
    broker.close_position("TEST", 101, START + timedelta(minutes=1), "test")
    snapshot = broker.broker_snapshot(START + timedelta(minutes=2))
    assert len(snapshot.day_positions) == 1
    p = snapshot.day_positions[0]
    assert p.signed_quantity == 0 and p.buy_quantity == p.sell_quantity == 10
    assert p.realised_gross == 10 and p.unrealised_gross == 0
    tomorrow = broker.broker_snapshot(START + timedelta(days=1))
    assert (
        not tomorrow.day_positions
        and not tomorrow.fills
        and not tomorrow.current_orders
    )


def test_paired_legacy_control_uses_actual_legacy_decisions():
    result = run_paired_case(
        case=case(),
        market_data=data(),
        candidate_policy=ExitPolicy(),
        control_policy=ExitPolicy(),
        control_mode="LEGACY_REPAIRED",
        strategy_config={},
        test_start=DAY,
        test_end=END,
        source_revision="test",
    )
    assert result["control_mode"] == "LEGACY_REPAIRED"
    assert result["control"]["legacy_control_decisions"]
    assert result["candidate"]["trades"][0]["exit_reason"] == "THESIS_BREAKOUT_FAILED"
    assert result["control"]["trades"][0]["exit_reason"] != "THESIS_BREAKOUT_FAILED"


def test_unknown_submission_cannot_become_an_assumed_fill(signals, monkeypatch):
    from backend.broker_models import OrderSubmissionUnknown

    def unknown(*args, **kwargs):
        raise OrderSubmissionUnknown("synthetic unknown acknowledgement")

    monkeypatch.setattr(SimulatedBroker, "submit_coordinator_order", unknown)
    with pytest.raises(RuntimeError, match="unknown entry submission"):
        run()


def test_stale_snapshot_keeps_original_mark_time_and_blocks_admission():
    broker = SimulatedBroker()
    broker.place_market_order("TEST", "BUY", 1, 100, START, {"stopLoss": 98})
    snapshot = broker.broker_snapshot(START + timedelta(minutes=10))
    assert snapshot.positions[0].mark_time == START
    assert not snapshot.entry_ready_at(START + timedelta(minutes=10))


def test_control_resolves_frozen_live_review_settings(tmp_path):
    from backend.backtesting.legacy_control import LegacyControlRunner
    from backend.journal import TradeJournal
    from backend.order_lifecycle import OrderLifecycleCoordinator

    journal = TradeJournal(str(tmp_path / "legacy.db"))
    try:
        runner = LegacyControlRunner(
            broker=SimulatedBroker(),
            coordinator=OrderLifecycleCoordinator(journal),
            risk_config={
                "positionRevalWeakExitMins": 60,
                "positionRevalIntervalMins": 20,
                "positionRevalBreakevenMins": 45,
            },
        )
        assert runner.legacy_policy.weak_exit_minutes == 60
        assert runner.legacy_policy.review_interval_minutes == 20
    finally:
        journal._get_conn().close()


def test_gap_fill_beyond_stop_keeps_hard_management_and_excludes_invalid_r(signals):
    candles = frames()
    candles["TEST"].loc[1, ["open", "high", "low", "close"]] = [96, 96.2, 95.8, 96]
    result = run(candles)
    branch = result["candidate"]
    assert any(
        item["reason"] == "INVALID_INITIAL_R_AFTER_GAP"
        for item in branch["excluded_entry_checkpoints"]
    )
    assert branch["execution_results"]
    assert branch["trades"]
    assert branch["parity"]["mismatches"] == 0


def test_generated_thesis_retains_full_frozen_policy_configuration(signals):
    result = run()
    thesis = result["candidate"]["entry_checkpoints"][0]["positions"][0]["thesis"]
    values = thesis["policy_snapshot"]["values"]
    assert values["risk"]["maxDailyLoss"] == 2000
    assert values["strategies"] == {}
    assert values["marketContext"]["primary_interval_minutes"] == 5
    assert values["exitManagement"]["policyVersion"] == ExitPolicy().policy_version


def test_terminal_checkpoint_at_new_entry_cutoff_remains_executable():
    from backend.tests.test_candidate_execution import START as FILLED

    seed = case()
    terminal = FILLED.replace(hour=9, minute=30)
    seed["checkpoint_at"] = terminal.isoformat()
    seed["positions"][0]["thesis"]["fill_binding"]["entry_terminal_at"] = (
        terminal.isoformat()
    )
    result = run_paired_case(
        case=seed,
        market_data=data(),
        candidate_policy=ExitPolicy(),
        control_policy=ExitPolicy(),
        test_start=DAY,
        test_end=END,
        source_revision="test",
    )
    assert result["paired_counts"]["total"] == 1


def test_tick_collapse_rejects_entry_like_live(monkeypatch):
    def evaluate(**kw):
        return [
            {
                "tradingsymbol": kw["symbol"],
                "direction": "BUY",
                "entryPrice": 100.01,
                "stopLoss": 100.02,
                "target": 110,
                "signal_score": 80,
            }
        ]

    monkeypatch.setattr(study, "evaluate_production_entries", evaluate)
    result = run()
    assert not result["candidate"]["fills"]
    assert any(
        r["reason"] == "INVALID_DIRECTIONAL_STOP"
        for r in result["candidate"]["admissions"]
    )


def test_causal_initial_calibration_and_live_probability_gate(monkeypatch):
    historical = [
        {
            "strategy": "fixture",
            "confidence": 80,
            "direction": "BUY",
            "entry_price": 100,
            "exit_price": 99,
            "stop_loss": 98,
            "status": "CLOSED",
            "net_pnl": -10,
            "financial_quality": "RECONCILED",
            "exit_time": (START - timedelta(days=1)).isoformat(),
            "recorded_at": (START - timedelta(days=1)).isoformat(),
        }
        for _ in range(10)
    ]
    seen = []

    def evaluate(**kw):
        probability, size = kw["calibration_lookup"]("fixture", 80)
        seen.append((probability, size))
        return [
            {
                "tradingsymbol": kw["symbol"],
                "direction": "BUY",
                "entryPrice": 100,
                "stopLoss": 98,
                "target": 110,
                "signal_score": 80,
                "estimated_probability": probability,
            }
        ]

    monkeypatch.setattr(study, "evaluate_production_entries", evaluate)
    result = run(calibration_history=historical)
    assert seen and set(seen) == {(0, 10)}
    assert not result["candidate"]["fills"]
    assert any(
        r["reason"] == "ESTIMATED_PROBABILITY_BELOW_0_60"
        for r in result["candidate"]["admissions"]
    )
    historical[0]["recorded_at"] = (START + timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="known before scoring"):
        run(calibration_history=historical)


def test_generated_checkpoint_preserves_first_fill_holding_age(signals):
    result = run()
    seed = result["control"]["entry_checkpoints"][0]
    first = seed["positions"][0]["first_entry_fill_at"]
    assert datetime.fromisoformat(first) < datetime.fromisoformat(seed["checkpoint_at"])
    paired = run_paired_case(
        case=seed,
        market_data=frames(),
        candidate_policy=ExitPolicy(),
        control_policy=ExitPolicy(),
        test_start=START,
        test_end=START + timedelta(hours=7),
        source_revision="test",
        control_mode="LEGACY_REPAIRED",
        strategy_config={},
    )
    assert datetime.fromisoformat(
        paired["control"]["trades"][0]["entry_time"]
    ) == datetime.fromisoformat(first)


def test_legacy_target_uses_fresh_quote_without_normal_context(tmp_path):
    from dataclasses import replace

    from backend.backtesting.legacy_control import LegacyControlRunner
    from backend.exit_management.profiles import ObjectiveMode, resolve_profile
    from backend.tests.test_candidate_execution import START as FILLED
    from backend.tests.test_candidate_execution import SYMBOL, _runner

    seed = _runner(tmp_path)
    managed = seed.positions[SYMBOL]
    managed.thesis = replace(
        managed.thesis,
        management_profile=replace(
            managed.thesis.management_profile,
            values={
                **managed.thesis.management_profile.values,
                "objective_mode": "structure_runner",
            },
        ),
    )
    profile = resolve_profile(managed.thesis.management_profile)
    policy = ExitPolicy(
        profile_overrides={
            profile.name.value: replace(
                profile, objective_mode=ObjectiveMode.STRUCTURE_RUNNER
            )
        }
    )
    runner = LegacyControlRunner(broker=seed.broker, coordinator=seed.coordinator)
    runner.register_position(thesis=managed.thesis, state=managed.state, policy=policy)
    at = FILLED + timedelta(seconds=1)
    runner.broker.mark_price(SYMBOL, 111, at)
    runner.on_event(at)
    assert runner.legacy_records[-1]["reason"] == "LEGACY_CONTROL_TARGET"
    assert runner.execution_results[-1]["intent_id"]
    runner.coordinator.journal._get_conn().close()


def test_interleaved_context_cache_preserves_separate_account_outcomes(
    signals, monkeypatch
):
    built = []
    original_build = study.MarketContextService.build

    def build(self, *args, **kwargs):
        built.append(args[0])
        return original_build(self, *args, **kwargs)

    monkeypatch.setattr(study.MarketContextService, "build", build)
    shared = run(frames(stop_at=2))
    shared_count = len(built)
    original_branch = study._branch

    def independent(**kwargs):
        kwargs["context_cache"] = {}
        return original_branch(**kwargs)

    monkeypatch.setattr(study, "_branch", independent)
    built.clear()
    separate = run(frames(stop_at=2))
    assert len(built) == 2 * shared_count
    for name in ("candidate", "control"):
        assert shared[name]["trades"] == separate[name]["trades"]
        assert shared[name]["equity_curve"] == separate[name]["equity_curve"]
        assert shared[name]["admissions"] == separate[name]["admissions"]
