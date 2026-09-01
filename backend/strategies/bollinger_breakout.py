import pandas as pd
from ta.volatility import BollingerBands
from typing import List, Dict, Any
from .base import BaseStrategy

class BollingerBreakoutStrategy(BaseStrategy):
    def get_name(self) -> str:
        return "Bollinger Breakout"
        
    def get_description(self) -> str:
        return "Price breaks out of Bollinger Bands with high volume"
        
    def calculate_signals(self, df: pd.DataFrame, tradingsymbol: str) -> List[Dict[str, Any]]:
        if len(df) < 25:
            return []
            
        signals = []
        df = df.copy()
        
        bb = BollingerBands(close=df['close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()
        df['vol_sma'] = df['volume'].rolling(window=20).mean()
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        vol_confirmed = last['volume'] > (last['vol_sma'] * 1.5) # 50% volume spike
        
        # BUY Condition (Breakout above upper band)
        if prev['close'] <= prev['bb_high'] and last['close'] > last['bb_high'] and vol_confirmed:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "BUY")
            target = self.calculate_target(entry, sl)
            
            signals.append(self.format_signal(
                tradingsymbol, "BUY", 80, entry, sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0,
                "Bollinger upper band breakout with volume spike",
                {"bb_high": last['bb_high']}
            ))
            
        # SELL Condition (Breakdown below lower band)
        elif prev['close'] >= prev['bb_low'] and last['close'] < last['bb_low'] and vol_confirmed:
            entry = last['close']
            sl = self.calculate_stop_loss(entry, "SELL")
            target = self.calculate_target(entry, sl)
            
            signals.append(self.format_signal(
                tradingsymbol, "SELL", 80, entry, sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0,
                "Bollinger lower band breakdown with volume spike",
                {"bb_low": last['bb_low']}
            ))
            
        return signals
