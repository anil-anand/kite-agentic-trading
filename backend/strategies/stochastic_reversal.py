import pandas as pd
from ta.momentum import StochasticOscillator
from typing import List, Dict, Any
from .base import BaseStrategy

class StochasticReversalStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Stochastic Reversal"
        
    def get_description(self) -> str:
        return "Stochastic %K crosses %D in overbought/oversold regions"
        
    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str) -> List[Dict[str, Any]]:
        if len(df) < 20:
            return []
            
        signals = []
        df = df.copy()
        
        stoch = StochasticOscillator(high=df['high'], low=df['low'], close=df['close'], window=14, smooth_window=3)
        df['stoch_k'] = stoch.stoch()
        df['stoch_d'] = stoch.stoch_signal()
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        # BUY Condition (Oversold cross)
        if prev['stoch_k'] <= prev['stoch_d'] and last['stoch_k'] > last['stoch_d'] and last['stoch_k'] < 25:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "BUY")
            target = self.calculate_target(entry, sl)
            
            signals.append(self.format_signal(
                tradingsymbol, "BUY", 75, entry, sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0,
                "Stochastic bullish crossover in oversold region",
                {"stoch_k": last['stoch_k'], "stoch_d": last['stoch_d']}
            ))
            
        # SELL Condition (Overbought cross)
        elif prev['stoch_k'] >= prev['stoch_d'] and last['stoch_k'] < last['stoch_d'] and last['stoch_k'] > 75:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "SELL")
            target = self.calculate_target(entry, sl)
            
            signals.append(self.format_signal(
                tradingsymbol, "SELL", 75, entry, sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0,
                "Stochastic bearish crossover in overbought region",
                {"stoch_k": last['stoch_k'], "stoch_d": last['stoch_d']}
            ))
            
        return signals
