from .base import BaseStrategy
from ta.momentum import TSIIndicator

class TSICrossStrategy(BaseStrategy):
    def get_name(self) -> str: return "TSI Crossover"
    def get_description(self) -> str: return "True Strength Index zero-line crossover for trend confirmation."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 30: return []
        tsi = TSIIndicator(close=df['close'])
        df['tsi'] = tsi.tsi()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['tsi'] < 0 and last['tsi'] >= 0:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.8)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "BUY", 76, last['close'], sl, target, 1.5, "TSI crossed above zero line", {}))
        elif prev['tsi'] > 0 and last['tsi'] <= 0:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.8)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 76, last['close'], sl, target, 1.5, "TSI crossed below zero line", {}))
        return signals
