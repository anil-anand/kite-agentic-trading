import pandas as pd
import numpy as np
from ta.volatility import AverageTrueRange
from ta.trend import ADXIndicator
from typing import List, Dict, Any
from .base import BaseStrategy

class SupertrendStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Supertrend"
        
    def get_description(self) -> str:
        return "Supertrend(10,3) crossover with ADX confirmation"
        
    def calculate_supertrend(self, df: pd.DataFrame, period=10, multiplier=3):
        atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=period).average_true_range()
        
        hl2 = (df['high'] + df['low']) / 2
        final_upperband = hl2 + (multiplier * atr)
        final_lowerband = hl2 - (multiplier * atr)
        
        supertrend = [True] * len(df)
        
        for i in range(1, len(df)):
            curr, prev = i, i-1
            
            if df['close'][curr] > final_upperband[prev]:
                supertrend[curr] = True
            elif df['close'][curr] < final_lowerband[prev]:
                supertrend[curr] = False
            else:
                supertrend[curr] = supertrend[prev]
                
                if supertrend[curr] and final_lowerband[curr] < final_lowerband[prev]:
                    final_lowerband[curr] = final_lowerband[prev]
                if not supertrend[curr] and final_upperband[curr] > final_upperband[prev]:
                    final_upperband[curr] = final_upperband[prev]
                    
        return supertrend, final_lowerband, final_upperband

    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str) -> List[Dict[str, Any]]:
        if len(df) < 30:
            return []
            
        signals = []
        df = df.copy()
        
        adx = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14).adx()
        df['adx'] = adx
        
        supertrend, lowerband, upperband = self.calculate_supertrend(df, 10, 3)
        df['supertrend'] = supertrend
        df['lowerband'] = lowerband
        df['upperband'] = upperband
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if last['adx'] > 25:
            # BUY Condition
            if not prev['supertrend'] and last['supertrend']:
                entry = last['close']
                sl = last['lowerband']
                target = self.calculate_target(entry, sl)
                
                confidence = min(100, int(70 + (last['adx'] - 25)))
                
                signals.append(self.format_signal(
                    tradingsymbol, "BUY", confidence, entry, sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0,
                    f"Supertrend turned bullish, ADX {last['adx']:.1f}",
                    {"adx": last['adx'], "lowerband": last['lowerband']}
                ))
                
            # SELL Condition
            elif prev['supertrend'] and not last['supertrend']:
                entry = last['close']
                sl = last['upperband']
                target = self.calculate_target(entry, sl)
                
                confidence = min(100, int(70 + (last['adx'] - 25)))
                
                signals.append(self.format_signal(
                    tradingsymbol, "SELL", confidence, entry, sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0,
                    f"Supertrend turned bearish, ADX {last['adx']:.1f}",
                    {"adx": last['adx'], "upperband": last['upperband']}
                ))
                
        return signals
