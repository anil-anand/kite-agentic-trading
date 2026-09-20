"""Regression coverage for the phase-1 pre-commit review corrections."""

from datetime import timedelta

from kiteconnect import KiteConnect

from backend.broker_models import (
    BrokerSnapshot,
    ExecutionNamespace,
    SnapshotQuality,
    normalize_fills_response,
    normalize_orders_response,
    normalize_positions_response,
)
from backend.config import config_manager
from backend.journal import TradeJournal
from backend.kite_client import KiteClient
from backend.risk_manager import RiskManager
from backend.time_utils import now_utc


def _snapshot(*, positions=None, orders=None, fills=None, positions_at=None):
    fetched = now_utc()
    positions_at = positions_at or fetched
    position_result = normalize_positions_response(
        {"net": positions or [], "day": positions or []},
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        fetched_at=positions_at,
    )
    order_result = normalize_orders_response(
        orders or [],
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        fetched_at=fetched,
    )
    fill_result = normalize_fills_response(
        fills or [],
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        fetched_at=fetched,
    )
    return BrokerSnapshot(
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        positions=position_result.net,
        day_positions=position_result.day,
        current_orders=order_result.orders,
        fills=fill_result.fills,
        positions_quality=position_result.quality,
        orders_quality=order_result.quality,
        fills_quality=fill_result.quality,
        fetched_at=fetched,
        positions_fetched_at=positions_at,
        orders_fetched_at=fetched,
        fills_fetched_at=fetched,
    )


def _risk_config(**overrides):
    config = {
        "maxDailyLoss": 1000,
        "maxDailyTrades": 100,
        "maxTradesPerSymbolPerDay": 100,
        "maxSimultaneousPositions": 10,
        "maxGrossExposure": 500000,
        "maxNetExposure": 100000,
        "maxSingleSymbolExposure": 500000,
        "maxSectorExposure": 500000,
        "maxCorrelatedExposure": 500000,
        "correlationThreshold": 0.7,
    }
    config.update(overrides)
    return config


def test_historical_boundaries_are_serialized_in_exchange_wall_time(monkeypatch):
    client = KiteClient()
    kite = KiteConnect(api_key="offline-test-key")
    captured = {}

    def fake_get(route, url_args=None, params=None):
        captured.update(params or {})
        return {"candles": []}

    monkeypatch.setattr(kite, "_get", fake_get)
    monkeypatch.setattr(client, "kite", kite)
    client.get_historical_data(
        123,
        now_utc().replace(hour=4, minute=30, second=0, microsecond=0),
        now_utc().replace(hour=5, minute=30, second=0, microsecond=0),
        "5minute",
    )

    assert captured["from"] == "2026-09-20 10:00:00" or captured["from"].endswith(
        "10:00:00"
    )
    assert captured["to"].endswith("11:00:00")


def test_hard_loss_latches_from_fresh_positions_when_fills_are_unavailable(monkeypatch):
    config = _risk_config()
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda state: None)

    fetched = now_utc()
    positions = normalize_positions_response(
        {
            "net": [
                {
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 10,
                    "averagePrice": 100,
                    "lastPrice": 100,
                    "realised": -5000,
                    "unrealised": 0,
                    "timestamp": fetched.isoformat(),
                }
            ],
            "day": [
                {
                    "tradingsymbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 10,
                    "averagePrice": 100,
                    "lastPrice": 100,
                    "realised": -5000,
                    "unrealised": 0,
                    "timestamp": fetched.isoformat(),
                }
            ],
        },
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        fetched_at=fetched,
    )
    snapshot = BrokerSnapshot(
        namespace=ExecutionNamespace.LIVE,
        account_id="acct-A",
        positions=positions.net,
        day_positions=positions.day,
        current_orders=(),
        fills=(),
        positions_quality=SnapshotQuality.COMPLETE,
        orders_quality=SnapshotQuality.COMPLETE,
        fills_quality=SnapshotQuality.UNAVAILABLE,
        fetched_at=fetched,
        positions_fetched_at=fetched,
        orders_fetched_at=fetched,
        fills_fetched_at=fetched,
    )
    risk = RiskManager()
    risk.update_from_broker_snapshot(snapshot)

    assert risk.kill_switch_active is True
    assert risk.accounting_quality == "UNAVAILABLE"
    assert risk.can_trade()[0] is False

    # A later incomplete read cannot clear a previously latched hard stop.
    risk.kill_switch_active = True
    risk.update_from_broker_snapshot(snapshot)
    assert risk.kill_switch_active is True


def test_newer_signed_fills_are_applied_once_to_old_position_residual(monkeypatch):
    config = _risk_config()
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda state: None)
    monkeypatch.setattr(
        "backend.journal.journal.get_todays_trade_counts",
        lambda: {"total": 0, "by_symbol": {}},
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    risk._get_correlation = lambda *_: 0.0
    now = now_utc()
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "quantity": -800,
                "averagePrice": 100,
                "lastPrice": 100,
                "timestamp": now.isoformat(),
            }
        ],
        fills=[
            {
                "tradeId": "R1",
                "orderId": "REDUCE",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "transactionType": "BUY",
                "quantity": 800,
                "averagePrice": 100,
                "fillTimestamp": (now - timedelta(seconds=1)).isoformat(),
            },
            {
                "tradeId": "E1",
                "orderId": "ENTRY",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "transactionType": "BUY",
                "quantity": 800,
                "averagePrice": 100,
                "fillTimestamp": (now - timedelta(seconds=1)).isoformat(),
            },
        ],
        positions_at=now - timedelta(seconds=4),
    )
    accepted, reason = risk.can_accept_position("INFY", "BUY", 800, 100, snapshot)

    assert accepted is False
    assert reason.startswith("NET_EXPOSURE_LIMIT")


def test_newer_fill_without_a_position_row_still_consumes_exposure_capacity(
    monkeypatch,
):
    config = _risk_config()
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda state: None)
    monkeypatch.setattr(
        "backend.journal.journal.get_todays_trade_counts",
        lambda: {"total": 0, "by_symbol": {}},
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    risk._get_correlation = lambda *_: 0.0
    now = now_utc()
    snapshot = _snapshot(
        fills=[
            {
                "tradeId": "E1",
                "orderId": "ENTRY",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "transactionType": "BUY",
                "quantity": 800,
                "averagePrice": 100,
                "fillTimestamp": (now - timedelta(seconds=1)).isoformat(),
            }
        ],
        positions_at=now - timedelta(seconds=4),
    )
    accepted, reason = risk.can_accept_position("INFY", "BUY", 800, 100, snapshot)

    assert accepted is False
    assert reason.startswith("MARK_UNAVAILABLE")


def test_verified_completed_reduction_and_entry_do_not_create_phantom_exposure(
    monkeypatch,
):
    config = _risk_config()
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda state: None)
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "instrument_token": 111,
                "quantity": 0,
            }
        ],
        orders=[
            {
                "orderId": "REDUCE",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "SELL",
                "quantity": 10,
                "filledQuantity": 10,
                "pendingQuantity": 0,
                "averagePrice": 100,
                "orderType": "MARKET",
                "status": "COMPLETE",
                "role": "REDUCTION",
            },
            {
                "orderId": "ENTRY",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 10,
                "filledQuantity": 10,
                "pendingQuantity": 0,
                "averagePrice": 100,
                "orderType": "MARKET",
                "status": "COMPLETE",
                "role": "ENTRY",
            },
        ],
        fills=[
            {
                "tradeId": "R1",
                "orderId": "REDUCE",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "SELL",
                "quantity": 10,
                "averagePrice": 100,
            },
            {
                "tradeId": "E1",
                "orderId": "ENTRY",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 10,
                "averagePrice": 100,
            },
        ],
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    state, reason = risk._build_exposure_state(snapshot)

    assert reason == "OK"
    assert state.gross == 0
    assert state.net == 0
    assert state.occupied_keys == set()


def test_filled_reservation_keeps_count_slot_until_journal_consumes_it(monkeypatch):
    config = _risk_config(maxDailyTrades=1)
    monkeypatch.setattr(config_manager, "get_risk_config", lambda: config)
    monkeypatch.setattr(config_manager, "load_daily_risk_state", lambda: {})
    monkeypatch.setattr(config_manager, "save_daily_risk_state", lambda state: None)
    monkeypatch.setattr(
        "backend.journal.journal.get_todays_trade_counts",
        lambda: {"total": 0, "by_symbol": {}},
    )
    risk = RiskManager()
    risk.reconciliation_status = "RECONCILED"
    snapshot = _snapshot()
    reservation_id, reason = risk.reserve_entry(
        "RELIANCE", "BUY", 10, 100, broker_snapshot=snapshot
    )
    assert reservation_id is not None, reason
    risk.bind_entry_order(reservation_id, "ENTRY")

    filled = _snapshot(
        orders=[
            {
                "orderId": "ENTRY",
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "transactionType": "BUY",
                "quantity": 10,
                "filledQuantity": 10,
                "pendingQuantity": 0,
                "averagePrice": 100,
                "status": "COMPLETE",
            }
        ]
    )
    risk.reconcile_entry_reservations(filled)
    assert risk.pending_entry_reservation_count == 1

    risk.complete_entry_reservation(reservation_id)
    assert risk.pending_entry_reservation_count == 0


def test_unknown_mark_blocks_entry_but_timestamped_mark_is_admission_ready():
    snapshot = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 1,
                "averagePrice": 100,
                "lastPrice": 100,
            }
        ]
    )
    assert snapshot.entry_ready is False

    timestamped = _snapshot(
        positions=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 1,
                "averagePrice": 100,
                "lastPrice": 100,
                "timestamp": now_utc().isoformat(),
            }
        ]
    )
    assert timestamped.entry_ready is True


def test_last_exit_time_normalizes_mixed_legacy_and_current_timestamps(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    journal.open_trade("OLD", "RELIANCE", "NSE", "BUY", "MIS", "test", 100, 1, 95, 110)
    journal.open_trade("NEW", "RELIANCE", "NSE", "BUY", "MIS", "test", 100, 1, 95, 110)
    conn = journal._get_conn()
    conn.execute(
        "UPDATE trades SET exit_time = ? WHERE id = ?",
        ("2026-09-20 10:00:00", "OLD"),
    )
    conn.execute(
        "UPDATE trades SET exit_time = ? WHERE id = ?",
        ("2026-09-20T05:00:00+00:00", "NEW"),
    )

    assert journal.get_last_exit_time("RELIANCE").hour == 5
