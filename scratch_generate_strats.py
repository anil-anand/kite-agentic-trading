import os

strats = {
    "adx_momentum": """from .base import BaseStrategy
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
                signals.append(self.format_signal(symbol, "BUY", 85, last['close'], sl, target, 1.5, "ADX crossed +DI for strong uptrend", {}))
            elif prev['-di'] <= prev['+di'] and last['-di'] > last['+di']:
                sl = self.calculate_stop_loss(last['close'], "SELL", 0.5)
                target = self.calculate_target(last['close'], sl, 1.5)
                signals.append(self.format_signal(symbol, "SELL", 85, last['close'], sl, target, 1.5, "ADX crossed -DI for strong downtrend", {}))
        return signals
""",
    "psar_trend": """from .base import BaseStrategy
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
            signals.append(self.format_signal(symbol, "BUY", 80, last['close'], sl, target, 1.5, "PSAR flipped below price", {}))
        elif pd.isna(prev['psar_down']) and not pd.isna(last['psar_down']):
            sl = last['psar_down']
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 80, last['close'], sl, target, 1.5, "PSAR flipped above price", {}))
        return signals
""",
    "donchian_breakout": """from .base import BaseStrategy
from ta.volatility import DonchianChannel

class DonchianBreakoutStrategy(BaseStrategy):
    def get_name(self) -> str: return "Donchian Breakout"
    def get_description(self) -> str: return "Trades when price breaks the 20-period high or low."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 25: return []
        dc = DonchianChannel(high=df['high'], low=df['low'], close=df['close'], window=20)
        df['dc_high'] = dc.donchian_channel_hband()
        df['dc_low'] = dc.donchian_channel_lband()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if last['close'] > prev['dc_high']:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.5)
            target = self.calculate_target(last['close'], sl, 2.0)
            signals.append(self.format_signal(symbol, "BUY", 75, last['close'], sl, target, 2.0, "Price broke 20-period Donchian high", {}))
        elif last['close'] < prev['dc_low']:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.5)
            target = self.calculate_target(last['close'], sl, 2.0)
            signals.append(self.format_signal(symbol, "SELL", 75, last['close'], sl, target, 2.0, "Price broke 20-period Donchian low", {}))
        return signals
""",
    "cci_reversal": """from .base import BaseStrategy
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
            signals.append(self.format_signal(symbol, "BUY", 72, last['close'], sl, target, 1.5, "CCI crossed above -100 (Oversold reversal)", {}))
        elif prev['cci'] > 100 and last['cci'] <= 100:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.8)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 72, last['close'], sl, target, 1.5, "CCI crossed below 100 (Overbought reversal)", {}))
        return signals
""",
    "williams_r": """from .base import BaseStrategy
from ta.momentum import WilliamsRIndicator

class WilliamsRStrategy(BaseStrategy):
    def get_name(self) -> str: return "Williams %R"
    def get_description(self) -> str: return "Momentum strategy trading reversals from -80 and -20 levels."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 20: return []
        wr = WilliamsRIndicator(high=df['high'], low=df['low'], close=df['close'], lbp=14)
        df['wr'] = wr.williams_r()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['wr'] < -80 and last['wr'] >= -80:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.6)
            target = self.calculate_target(last['close'], sl, 1.2)
            signals.append(self.format_signal(symbol, "BUY", 70, last['close'], sl, target, 1.2, "Williams %R crossed above -80", {}))
        elif prev['wr'] > -20 and last['wr'] <= -20:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.6)
            target = self.calculate_target(last['close'], sl, 1.2)
            signals.append(self.format_signal(symbol, "SELL", 70, last['close'], sl, target, 1.2, "Williams %R crossed below -20", {}))
        return signals
""",
    "mfi_exhaustion": """from .base import BaseStrategy
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
            signals.append(self.format_signal(symbol, "BUY", 78, last['close'], sl, target, 1.5, "MFI volume exhaustion bounce from 20", {}))
        elif prev['mfi'] > 80 and last['mfi'] <= 80:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.7)
            target = self.calculate_target(last['close'], sl, 1.5)
            signals.append(self.format_signal(symbol, "SELL", 78, last['close'], sl, target, 1.5, "MFI volume exhaustion rejection from 80", {}))
        return signals
""",
    "keltner_breakout": """from .base import BaseStrategy
from ta.volatility import KeltnerChannel

class KeltnerBreakoutStrategy(BaseStrategy):
    def get_name(self) -> str: return "Keltner Channel Breakout"
    def get_description(self) -> str: return "Momentum breakout beyond the upper or lower Keltner bands."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 25: return []
        kc = KeltnerChannel(high=df['high'], low=df['low'], close=df['close'], window=20, window_atr=10)
        df['kc_h'] = kc.keltner_channel_hband()
        df['kc_l'] = kc.keltner_channel_lband()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['close'] <= prev['kc_h'] and last['close'] > last['kc_h']:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.5)
            target = self.calculate_target(last['close'], sl, 2.0)
            signals.append(self.format_signal(symbol, "BUY", 82, last['close'], sl, target, 2.0, "Broke above Upper Keltner Channel", {}))
        elif prev['close'] >= prev['kc_l'] and last['close'] < last['kc_l']:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.5)
            target = self.calculate_target(last['close'], sl, 2.0)
            signals.append(self.format_signal(symbol, "SELL", 82, last['close'], sl, target, 2.0, "Broke below Lower Keltner Channel", {}))
        return signals
""",
    "awesome_oscillator": """from .base import BaseStrategy
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
""",
    "tsi_cross": """from .base import BaseStrategy
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
""",
    "stoc_rsi": """from .base import BaseStrategy
from ta.momentum import StochRSIIndicator

class StochRSIStrategy(BaseStrategy):
    def get_name(self) -> str: return "Stochastic RSI"
    def get_description(self) -> str: return "Extremely sensitive momentum strategy trading 20/80 boundaries."
    
    def calculate_signals(self, df, symbol: str):
        if len(df) < 20: return []
        stoch_rsi = StochRSIIndicator(close=df['close'], window=14, smooth1=3, smooth2=3)
        df['srsi_k'] = stoch_rsi.stochrsi_k()
        df['srsi_d'] = stoch_rsi.stochrsi_d()
        
        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if prev['srsi_k'] < 0.20 and last['srsi_k'] >= 0.20 and last['srsi_k'] > last['srsi_d']:
            sl = self.calculate_stop_loss(last['close'], "BUY", 0.5)
            target = self.calculate_target(last['close'], sl, 1.2)
            signals.append(self.format_signal(symbol, "BUY", 77, last['close'], sl, target, 1.2, "StochRSI K crossed above 0.20", {}))
        elif prev['srsi_k'] > 0.80 and last['srsi_k'] <= 0.80 and last['srsi_k'] < last['srsi_d']:
            sl = self.calculate_stop_loss(last['close'], "SELL", 0.5)
            target = self.calculate_target(last['close'], sl, 1.2)
            signals.append(self.format_signal(symbol, "SELL", 77, last['close'], sl, target, 1.2, "StochRSI K crossed below 0.80", {}))
        return signals
"""
}

base_dir = "/Users/anilanand/dev/personal/kite-agentic-trading/backend/strategies"

for filename, content in strats.items():
    filepath = os.path.join(base_dir, f"{filename}.py")
    with open(filepath, "w") as f:
        f.write(content)
    print(f"Created {filepath}")
