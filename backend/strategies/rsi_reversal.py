import pandas as pd
from ta.momentum import RSIIndicator
from ta.volume import VolumeWeightedAveragePrice
from typing import List, Dict, Any
from .base import BaseStrategy

class RSIReversalStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "RSI Mean Reversion"
        
    def get_description(self) -> str:
        return "RSI recovers from extremes (<30 or >70) with VWAP context"
        
    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str) -> List[Dict[str, Any]]:
        if len(df) < 20:
            return []
            
        signals = []
        df = df.copy()
        
        rsi = RSIIndicator(close=df['close'], window=14).rsi()
        vwap = VolumeWeightedAveragePrice(
            high=df['high'], low=df['low'], close=df['close'], volume=df['volume']
        ).volume_weighted_average_price()
        
        df['rsi'] = rsi
        df['vwap'] = vwap
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        # BUY Condition
        if prev['rsi'] < 30 and last['rsi'] >= 30 and last['close'] > last['vwap']:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "BUY")
            target = self.calculate_target(entry, sl)
            
            confidence = int(70 + (30 - prev['rsi']))
            confidence = min(100, max(50, confidence))
            
            signals.append(self.format_signal(
                tradingsymbol, "BUY", confidence, entry, sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0,
                f"RSI crossed above 30 from {prev['rsi']:.1f}, price above VWAP",
                {"rsi": last['rsi'], "vwap": last['vwap']}
            ))
            
        # SELL Condition
        elif prev['rsi'] > 70 and last['rsi'] <= 70 and last['close'] < last['vwap']:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "SELL")
            target = self.calculate_target(entry, sl)
            
            confidence = int(70 + (prev['rsi'] - 70))
            confidence = min(100, max(50, confidence))
            
            signals.append(self.format_signal(
                tradingsymbol, "SELL", confidence, entry, sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0,
                f"RSI crossed below 70 from {prev['rsi']:.1f}, price below VWAP",
                {"rsi": last['rsi'], "vwap": last['vwap']}
            ))
            
        return signals
