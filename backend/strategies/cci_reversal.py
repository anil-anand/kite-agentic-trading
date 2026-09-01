from .base import BaseStrategy
from ta.trend import CCIIndicator

class CCIReversalStrategy(BaseStrategy):
    def get_name(self) -> str: return "CCI Reversal"
    def get_description(self) -> str: return "Mean reversion when CCI crosses back from extremes (+/- 100)."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 20: return []
        cci = CCIIndicator(high=df['high'], low=df['low'], close=df['close'], window=20)
        df['cci'] = cci.cci()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['cci'] < -100 and last['cci'] >= -100:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.8)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "BUY", 72, last['close'], sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0, "CCI crossed above -100 (Oversold reversal)", {}))
        elif prev['cci'] > 100 and last['cci'] <= 100:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.8)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 72, last['close'], sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0, "CCI crossed below 100 (Overbought reversal)", {}))
        return signals
