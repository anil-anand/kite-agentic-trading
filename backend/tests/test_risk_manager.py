from backend.config import config_manager
from backend.risk_manager import risk_manager


def test_calculate_position_size_uses_stop_distance(monkeypatch):
    risk_cfg = config_manager.get_risk_config().copy()
    risk_cfg["maxCapitalPerTrade"] = 10_000
    risk_cfg["riskPerTrade"] = 100
    monkeypatch.setitem(config_manager.config, "risk", risk_cfg)

    tight_stop_qty = risk_manager.calculate_position_size(100, 95)
    wide_stop_qty = risk_manager.calculate_position_size(100, 90)

    assert tight_stop_qty == 20
    assert wide_stop_qty == 10


def test_calculate_position_size_caps_by_capital(monkeypatch):
    risk_cfg = config_manager.get_risk_config().copy()
    risk_cfg["maxCapitalPerTrade"] = 1_000
    risk_cfg["riskPerTrade"] = 1_000
    monkeypatch.setitem(config_manager.config, "risk", risk_cfg)

    # Risk sizing suggests 200 shares (risk 1000 / stop distance 5),
    # but max capital at ₹100 allows only 10 shares.
    assert risk_manager.calculate_position_size(100, 95) == 10


def test_calculate_position_size_caps_by_available_margin(monkeypatch):
    risk_cfg = config_manager.get_risk_config().copy()
    risk_cfg["maxCapitalPerTrade"] = 10_000
    risk_cfg["riskPerTrade"] = 1_000
    monkeypatch.setitem(config_manager.config, "risk", risk_cfg)

    assert risk_manager.calculate_position_size(100, 95, available_margin=350) == 3


def test_calculate_position_size_returns_zero_when_margin_cannot_fund_one_share(
    monkeypatch,
):
    risk_cfg = config_manager.get_risk_config().copy()
    risk_cfg["maxCapitalPerTrade"] = 10_000
    risk_cfg["riskPerTrade"] = 1_000
    monkeypatch.setitem(config_manager.config, "risk", risk_cfg)

    assert risk_manager.calculate_position_size(100, 95, available_margin=99) == 0
