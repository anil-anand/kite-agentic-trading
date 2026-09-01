from .base import BaseStrategy
from ta.trend import ADXIndicator

class ADXMomentumStrategy(BaseStrategy):
    def get_name(self) -> str: return "ADX Momentum"
    def get_description(self) -> str: return "Buys when +DI crosses -DI with ADX > 25, indicating strong trend."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 30: return []
        adxI = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
        df['adx'] = adxI.adx()
        df['+di'] = adxI.adx_pos()
        df['-di'] = adxI.adx_neg()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if last['adx'] > 25:
            if prev['+di'] <= prev['-di'] and last['+di'] > last['-di']:
                sl = self.calculate_stop_loss(last['close'], "BUY", 0.5)
                target = self.calculate_target(last['close'], sl, 1.5)
                signals.append(self.format_signal(symbol, "BUY", 85, last['close'], sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0, "ADX crossed +DI for strong uptrend", {}))
            elif prev['-di'] <= prev['+di'] and last['-di'] > last['+di']:
                sl = self.calculate_stop_loss(last['close'], "SELL", 0.5)
                target = self.calculate_target(last['close'], sl, 1.5)
                signals.append(self.format_signal(symbol, "SELL", 85, last['close'], sl, target, round(abs(target-last['close'])/abs(last['close']-sl), 2) if last['close'] != sl else 0, "ADX crossed -DI for strong downtrend", {}))
        return signals
