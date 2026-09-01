import logging
from typing import List

from .kite_client import kite_client
from .nifty_universe import NIFTY_50


class DynamicScreener:
    def __init__(self):
        self.daily_watchlist = []

    def generate_daily_watchlist(
        self, universe: List[str] = NIFTY_50, limit: int = 10
    ) -> List[str]:
        """
        AI/Algorithmic screener that selects the best stocks to trade today based on volatility,
        gaps, and relative volume using a single batched quote API call.
        """
        try:
            # Prefix with exchange for the quote API
            instruments = [f"NSE:{symbol}" for symbol in universe]

            # Fetch full quote for all instruments in one efficient API call
            quotes = kite_client.get_quote(instruments)
            if not quotes:
                return universe[:limit]

            scored_stocks = []

            for symbol, data in quotes.items():
                if "last_price" not in data or "ohlc" not in data:
                    continue

                ltp = data["last_price"]
                open_price = data["ohlc"]["open"]
                prev_close = data["ohlc"]["close"]
                volume = data.get("volume", 0)

                # We need some movement to trade. Avoid flat stocks.
                if prev_close == 0 or open_price == 0:
                    continue

                # 1. Gap Percentage (Overnight movement)
                gap_pct = abs((open_price - prev_close) / prev_close) * 100

                # 2. Intraday Movement (Open to LTP)
                intraday_pct = abs((ltp - open_price) / open_price) * 100

                # Score formula: We want high intraday movement and moderate to high gaps
                # This highlights stocks that are "In Play" today.
                score = (intraday_pct * 2.0) + gap_pct

                # Clean symbol name
                clean_symbol = symbol.replace("NSE:", "")

                scored_stocks.append(
                    {
                        "symbol": clean_symbol,
                        "score": score,
                        "volume": volume,
                        "intraday_pct": intraday_pct,
                    }
                )

            # Sort by our volatility/opportunity score
            scored_stocks.sort(key=lambda x: x["score"], reverse=True)

            # Select the top N stocks
            top_stocks = [stock["symbol"] for stock in scored_stocks[:limit]]
            self.daily_watchlist = top_stocks

            logging.info(f"Dynamic Screener selected top {limit} stocks: {top_stocks}")
            return top_stocks

        except Exception as e:
            logging.error(f"Failed to generate dynamic watchlist: {e}")
            return universe[:limit]


screener_engine = DynamicScreener()
