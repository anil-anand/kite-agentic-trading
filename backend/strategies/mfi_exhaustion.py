from .base import BaseStrategy
from ta.volume import MFIIndicator

class MFIExhaustionStrategy(BaseStrategy):
    def get_name(self) -> str: return "MFI Exhaustion"
    def get_description(self) -> str: return "Volume-weighted RSI trading exhaustion zones (<20 or >80)."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 20: return []
        mfi = MFIIndicator(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14)
        df['mfi'] = mfi.money_flow_index()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['mfi'] < 20 and last['mfi'] >= 20:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.7)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "BUY", 78, last['close'], sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0, "MFI volume exhaustion bounce from 20", {}))
        elif prev['mfi'] > 80 and last['mfi'] <= 80:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.7)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 78, last['close'], sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0, "MFI volume exhaustion rejection from 80", {}))
        return signals
