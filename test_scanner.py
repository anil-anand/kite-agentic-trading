import sys
sys.path.append('.')
from backend.scanner import scanner
import traceback

def test():
    symbols = ["MARUTI", "NIFTY 50", "RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK", "SBIN"]
    
    # Manually run all strategies
    try:
        from backend.kite_client import kite_client
        instruments = kite_client.get_instruments("NSE")
        instrument_map = {i['tradingsymbol']: i['instrument_token'] for i in instruments}
        
        for symbol in symbols:
            token = instrument_map.get(symbol)
            if not token: continue
            
            df, _ = scanner._fetch_candles(token, symbol)
            if df.empty: continue
            
            for strat_id, strategy in scanner.strategies.items():
                try:
                    signals = strategy.calculate_signals(df.copy(), symbol)
                except Exception as e:
                    print(f"Error in {strat_id} for {symbol}: {e}")
                    traceback.print_exc()
    except Exception as e:
        traceback.print_exc()

if __name__ == '__main__':
    test()
