"""Historical admission uses production risk rules without live IO or wall time."""

import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.accounting import AccountingService
from backend.broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    SnapshotQuality,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
)
from backend.risk_manager import RiskManager
from backend.session_clock import SessionClock, SessionPolicy
from backend.trading_costs import TradingCostCalculator

NOW = datetime(2020, 1, 6, 5, tzinfo=timezone.utc)


def config(**overrides):
    return {
        "maxDailyLoss": 1_000,
        "maxDailyTrades": 10,
        "maxTradesPerSymbolPerDay": 2,
        "maxSimultaneousPositions": 5,
        "maxGrossExposure": 200_000,
        "maxNetExposure": 100_000,
        "maxSingleSymbolExposure": 50_000,
        "maxSectorExposure": 75_000,
        "maxCorrelatedExposure": 75_000,
        "correlationThreshold": 0.7,
        "startTradeAfter": "09:45",
        "noNewTradesAfter": "15:00",
        "squareOffTime": "15:15",
        "maxCapitalPerTrade": 10_000,
        "leverageMultiplier": 1,
        "riskPerTrade": 100,
        **overrides,
    }


def manager(*, clock=None, **overrides):
    return RiskManager.for_research(
        **{
            "risk_config": config(),
            "clock": clock or (lambda: NOW),
            "trade_counts_provider": lambda _: {
                "total": 0,
                "by_symbol": {},
                "entry_order_ids": (),
            },
            "correlation_provider": lambda *_: 0,
            "sector_provider": lambda _: "TEST_SECTOR",
            **overrides,
        }
    )


def snapshot(*, at=NOW, quantity=0, price=100, mark=100, symbol="ABC"):
    positions, orders, fills = [], [], []
    if quantity:
        identity = {
            "tradingsymbol": symbol,
            "exchange": "NSE",
            "product": "MIS",
            "instrument_token": 1,
        }
        positions = [
            {
                **identity,
                "quantity": quantity,
                "average_price": price,
                "last_price": mark,
                "buy_quantity": quantity,
                "day_buy_quantity": quantity,
                "sell_quantity": 0,
                "buy_value": quantity * price,
                "sell_value": 0,
                "realised": 0,
                "unrealised": quantity * (mark - price),
                "timestamp": at,
            }
        ]
        orders = [
            {
                **identity,
                "order_id": "entry-1",
                "transaction_type": "BUY",
                "quantity": quantity,
                "filled_quantity": quantity,
                "pending_quantity": 0,
                "average_price": price,
                "price": price,
                "order_type": "MARKET",
                "status": "COMPLETE",
                "role": "ENTRY",
            }
        ]
        fills = [
            {
                **identity,
                "trade_id": "fill-1",
                "order_id": "entry-1",
                "transaction_type": "BUY",
                "quantity": quantity,
                "average_price": price,
                "fill_timestamp": at,
            }
        ]
    kwargs = {
        "namespace": ExecutionNamespace.REPLAY,
        "account_id": "isolated-account",
        "fetched_at": at,
    }
    normalized_positions = normalize_positions_response(
        {"net": positions, "day": positions}, **kwargs
    )
    normalized_orders = normalize_orders_response(orders, **kwargs)
    normalized_fills = normalize_fills_response(fills, **kwargs)
    return BrokerSnapshot(
        **kwargs,
        positions=normalized_positions.net,
        day_positions=normalized_positions.day,
        current_orders=normalized_orders.orders,
        fills=normalized_fills.fills,
        positions_quality=normalized_positions.quality,
        orders_quality=normalized_orders.quality,
        fills_quality=normalized_fills.quality,
        positions_fetched_at=at,
        orders_fetched_at=at,
        fills_fetched_at=at,
    )


def test_import_and_research_construction_do_not_import_live_dependencies():
    script = """
import importlib.abc
import sys
from datetime import datetime, timezone
class BlockLiveImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'backend.config', 'backend.kite_client', 'backend.journal'}:
            raise AssertionError('live dependency requested: ' + fullname)
sys.meta_path.insert(0, BlockLiveImports())
from backend.risk_manager import RiskManager, risk_manager
assert risk_manager._instance is None
risk = RiskManager.for_research(
    risk_config={'maxDailyLoss': 1000},
    clock=lambda: datetime(2020, 1, 6, 5, tzinfo=timezone.utc),
    trade_counts_provider=lambda _: {'total': 0, 'by_symbol': {}},
    correlation_provider=lambda *_: None,
    sector_provider=lambda _: 'UNKNOWN',
)
assert risk.calculate_position_size(100, 95, available_margin=10000) > 0
assert risk.date_str == '2020-01-06'
assert risk._trade_counts()['total'] == 0
assert risk._get_correlation('ABC', 'DEF') is None
assert risk_manager._instance is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_historical_freshness_uses_event_time_and_preserves_all_safety_checks():
    recorded = snapshot(quantity=1)
    assert recorded.entry_ready_at(NOW)
    assert not recorded.entry_ready_at(NOW + timedelta(seconds=121))
    assert not recorded.entry_ready_at(NOW - timedelta(seconds=6))
    assert not replace(recorded, account_id="UNKNOWN").entry_ready_at(NOW)
    assert not replace(recorded, fills_quality=SnapshotQuality.PARTIAL).entry_ready_at(
        NOW
    )
    assert not replace(
        recorded, orders_fetched_at=NOW - timedelta(seconds=6)
    ).entry_ready_at(NOW)
    assert not replace(
        recorded,
        positions=(replace(recorded.positions[0], mark_time=NOW - timedelta(hours=1)),),
    ).entry_ready_at(NOW)


def test_clock_and_snapshot_admission_require_aware_times():
    with pytest.raises(ValueError, match="aware"):
        manager(clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="aware"):
        snapshot().entry_ready_at(NOW.replace(tzinfo=None))


def test_isolated_risk_config_is_detached_from_caller_mutation():
    cfg = config()
    risk = manager(risk_config=cfg)
    cfg["riskPerTrade"] = 50_000
    assert risk.calculate_position_size(100, 95, available_margin=10_000) == 20


def test_historical_clock_controls_admission_and_reservation_creation():
    observed = [NOW]
    count_times = []

    def counts(at):
        count_times.append(at)
        return {"total": 0, "by_symbol": {}}

    risk = manager(clock=lambda: observed[0], trade_counts_provider=counts)
    risk.update_from_broker_snapshot(snapshot())
    assert risk.can_trade() == (True, "OK")
    reservation, reason = risk.reserve_entry("ABC", "BUY", 20, 100, snapshot())
    assert reservation, reason
    assert risk._entry_reservations[reservation].created_at == NOW
    assert count_times == [NOW]
    second, reason = risk.reserve_entry("ABC", "BUY", 1, 100, snapshot())
    assert second is None
    assert reason == "DUPLICATE_POSITION_OR_ENTRY: ABC"
    risk.release_entry_reservation(reservation)
    observed[0] = NOW.replace(hour=10)
    assert risk.can_trade()[0] is False
    observed[0] = NOW + timedelta(days=1)
    assert "session reset" in risk.can_trade()[1]


def test_consumed_daily_and_symbol_slots_use_isolated_journal_counts():
    risk = manager(
        risk_config=config(maxDailyTrades=1),
        trade_counts_provider=lambda _: {"total": 1, "by_symbol": {"ABC": 1}},
    )
    reservation, reason = risk.reserve_entry("DEF", "BUY", 1, 100, snapshot())
    assert reservation is None
    assert reason == "MAX_DAILY_TRADES_LIMIT"
    risk = manager(
        risk_config=config(maxTradesPerSymbolPerDay=1),
        trade_counts_provider=lambda _: {"total": 1, "by_symbol": {"ABC": 1}},
    )
    reservation, reason = risk.reserve_entry("ABC", "BUY", 1, 100, snapshot())
    assert reservation is None
    assert reason == "MAX_SYMBOL_TRADES_LIMIT: ABC"


def test_order_grouped_entry_fees_and_hard_loss_remain_admission_constraints():
    cost = AccountingService(TradingCostCalculator(brokerage_pct=0.01))
    risk = manager(risk_config=config(maxDailyLoss=100), accounting=cost)
    recorded = snapshot(quantity=100, mark=99)
    reservation, reason = risk.reserve_entry("DEF", "BUY", 1, 100, recorded)
    assert reservation is None
    assert reason == "DAILY_LOSS_LIMIT"
    fees = cost.fees_for_fills(recorded.fills).total
    assert risk.incurred_fees == fees
    assert risk.daily_pnl == -100 - fees
    assert risk.kill_switch_active
    risk.update_from_broker_snapshot(snapshot(quantity=100, mark=110))
    assert risk.kill_switch_active


def test_pending_reservation_uses_production_sector_and_capacity_constraints():
    risk = manager(risk_config=config(maxSectorExposure=150))
    first, _ = risk.reserve_entry("ABC", "BUY", 1, 100, snapshot())
    assert first
    second, reason = risk.reserve_entry("DEF", "BUY", 1, 100, snapshot())
    assert second is None
    assert reason == "SECTOR_EXPOSURE_LIMIT: TEST_SECTOR would exceed 150.0"
    risk.complete_entry_reservation(first)
    second, reason = risk.reserve_entry("DEF", "BUY", 1, 100, snapshot())
    assert second, reason


def test_correlation_receives_current_causal_time_without_daily_cache():
    observed = [NOW]
    calls = []

    def correlation(a, b, at):
        calls.append((a, b, at))
        return None if at == NOW else 0

    risk = manager(clock=lambda: observed[0], correlation_provider=correlation)
    first, _ = risk.reserve_entry("ABC", "BUY", 1, 100, snapshot())
    assert first
    second, reason = risk.reserve_entry("DEF", "BUY", 1, 100, snapshot())
    assert second is None
    assert reason == "CORRELATION_UNAVAILABLE: DEF/ABC"
    observed[0] += timedelta(seconds=1)
    second, reason = risk.reserve_entry("DEF", "BUY", 1, 100, snapshot(at=observed[0]))
    assert second, reason
    assert calls == [("DEF", "ABC", NOW), ("DEF", "ABC", observed[0])]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -1.01, 1.01])
def test_invalid_correlation_cannot_admit_risk(value):
    assert (
        manager(correlation_provider=lambda *_: value)._get_correlation("A", "B")
        is None
    )


@pytest.mark.parametrize(
    "counts", [None, {"total": -1, "by_symbol": {}}, {"total": 1, "by_symbol": {}}]
)
def test_inconsistent_journal_counts_cannot_admit_risk(counts):
    risk = manager(trade_counts_provider=lambda _: counts)
    with pytest.raises(ValueError, match="counts"):
        risk.reserve_entry("ABC", "BUY", 1, 100, snapshot())
    assert risk.pending_entry_reservation_count == 0


def test_no_broker_fallback_or_live_scope_in_research():
    risk = manager()
    with pytest.raises(RuntimeError, match="explicit snapshot"):
        risk.reconcile_state()
    with pytest.raises(ValueError, match="LIVE"):
        risk.update_from_broker_snapshot(
            replace(snapshot(), namespace=ExecutionNamespace.LIVE)
        )
    mixed = snapshot(quantity=100, mark=1)
    mixed = replace(
        mixed,
        positions=(
            replace(
                mixed.positions[0],
                key=replace(mixed.positions[0].key, account_id="another-account"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="another account"):
        risk.update_from_broker_snapshot(mixed)
    assert risk.daily_pnl == 0
    assert not risk.kill_switch_active


def test_session_loss_latch_requires_verified_rollover_even_with_injected_clock():
    observed = [NOW]
    risk = manager(
        clock=lambda: observed[0],
        initial_state={"date": NOW.date().isoformat(), "kill_switch_active": True},
    )
    observed[0] += timedelta(days=1)
    session = SessionClock(SessionPolicy()).snapshot(observed[0])
    assert not risk.rotate_session_if_verified(
        session, reconciliation_verified=False, has_residual_obligations=False
    )
    assert risk.kill_switch_active
    assert risk.rotate_session_if_verified(
        session, reconciliation_verified=True, has_residual_obligations=False
    )
    assert not risk.kill_switch_active
    assert risk.reconciliation_status == "RECONCILIATION_PENDING"
