from .base import BaseStrategy
from ta.momentum import AwesomeOscillatorIndicator

class AwesomeOscillatorStrategy(BaseStrategy):
    def get_name(self) -> str: return "Awesome Oscillator Zero Cross"
    def get_description(self) -> str: return "Trades momentum shifts when AO crosses the zero line."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 35: return []
        ao = AwesomeOscillatorIndicator(high=df['high'], low=df['low'])
        df['ao'] = ao.awesome_oscillator()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['ao'] < 0 and last['ao'] >= 0:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.6)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "BUY", 74, last['close'], sl, target, 1.5, "AO crossed above zero line", {}))
        elif prev['ao'] > 0 and last['ao'] <= 0:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.6)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 74, last['close'], sl, target, 1.5, "AO crossed below zero line", {}))
        return signals
