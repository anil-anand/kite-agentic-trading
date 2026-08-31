from .base import BaseStrategy
from ta.trend import PSARIndicator

class PSARTrendStrategy(BaseStrategy):
    def get_name(self) -> str: return "Parabolic SAR Trend"
    def get_description(self) -> str: return "Trades when Parabolic SAR flips to the opposite side of price."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 20: return []
        psar = PSARIndicator(high=df['high'], low=df['low'], close=df['close'])
        df['psar_up'] = psar.psar_up()
        df['psar_down'] = psar.psar_down()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        import pandas as pd
        if pd.isna(prev['psar_up']) and not pd.isna(last['psar_up']):
            sl = last['psar_up']
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "BUY", 80, last['close'], sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0, "PSAR flipped below price", {}))
        elif pd.isna(prev['psar_down']) and not pd.isna(last['psar_down']):
            sl = last['psar_down']
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 80, last['close'], sl, target, round(abs(target-entry)/abs(entry-sl), 2) if entry != sl else 0, "PSAR flipped above price", {}))
        return signals
