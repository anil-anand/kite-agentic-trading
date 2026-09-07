import pytest
from backend.trading_costs import cost_calculator

def test_calculate_leg_charges_buy():
    # Buy 100 shares at 1000 = 1,00,000 turnover
    charges = cost_calculator.calculate_leg_charges(1000.0, 100, "BUY")
    # Brokerage: min(0.0003 * 100000, 20) = min(30, 20) = 20.0
    assert charges["brokerage"] == 20.0
    # STT on buy for intraday is 0.0
    assert charges["stt"] == 0.0
    # Exchange txn: 100000 * 0.0000322 = 3.22
    assert charges["exchange_txn"] == 3.22
    # SEBI: 100000 * 0.000001 = 0.1
    assert charges["sebi"] == 0.1
    # GST: 18% of (20 + 3.22 + 0.1) = 18% of 23.32 = 4.2
    assert charges["gst"] == 4.2
    # Stamp: 100000 * 0.00003 = 3.0
    assert charges["stamp"] == 3.0
    # Total
    assert charges["total"] == round(20.0 + 0.0 + 3.22 + 0.1 + 4.2 + 3.0, 2)

def test_calculate_leg_charges_sell():
    # Sell 100 shares at 1010 = 1,01,000 turnover
    charges = cost_calculator.calculate_leg_charges(1010.0, 100, "SELL")
    # Brokerage: min(0.0003 * 101000, 20) = min(30.3, 20) = 20.0
    assert charges["brokerage"] == 20.0
    # STT on sell for intraday is 0.025% = 101000 * 0.00025 = 25.25 -> rounded to 25
    assert charges["stt"] == 25.0
    # Exchange txn: 101000 * 0.0000322 = 3.25
    assert charges["exchange_txn"] == 3.25
    # SEBI: 101000 * 0.000001 = 0.1
    assert charges["sebi"] == 0.1
    # GST: 18% of (20 + 3.25 + 0.1) = 18% of 23.35 = 4.2
    assert charges["gst"] == 4.2
    # Stamp on sell is 0.0
    assert charges["stamp"] == 0.0
    # Total
    assert charges["total"] == round(20.0 + 25.0 + 3.25 + 0.1 + 4.2 + 0.0, 2)

def test_calculate_trade_charges():
    # Buy 100 shares at 1000, sell at 1010.
    # Gross P&L = 1000
    res = cost_calculator.calculate_trade_charges(
        direction="BUY",
        entry_price=1000.0,
        exit_price=1010.0,
        quantity=100,
        signal_entry_price=999.0,   # Intended to buy at 999, actually bought at 1000 (slippage -100)
        signal_exit_price=1012.0    # Intended to sell at 1012, actually sold at 1010 (slippage -200)
    )
    
    assert res["gross_pnl"] == 1000.0
    assert res["slippage"] == -300.0
    
    # Check if total fees is approximately correct
    # Buy charges ~ 30.52
    # Sell charges ~ 52.55
    # Total ~ 83.07
    # Net P&L = 1000 - 83.07 = 916.93
    assert res["brokerage"] == 40.0
    assert res["taxes"] == 36.4  # stt(25) + stamp(3) + gst_buy(4.2) + gst_sell(4.2)
    assert res["exchange_charges"] == round(3.22 + 3.25 + 0.1 + 0.1, 2)
    assert res["net_pnl"] == round(1000.0 - res["total_fees"], 2)

def test_calculate_trade_charges_sell_first():
    # Short 100 shares at 1010, cover at 1000
    # Gross P&L = 1000
    res = cost_calculator.calculate_trade_charges(
        direction="SELL",
        entry_price=1010.0,
        exit_price=1000.0,
        quantity=100
    )
    assert res["gross_pnl"] == 1000.0
    # Brokerage: 40.0
    # STT on entry sell: 25.0
    # Stamp on exit buy: 3.0
    # Slippage should be 0.0
    assert res["slippage"] == 0.0
    assert res["net_pnl"] < res["gross_pnl"]
