import logging
from typing import List

from .config import config_manager
from .kite_client import kite_client
from .nifty_universe import NIFTY_100


class DynamicScreener:
    def __init__(self):
        self.daily_watchlist = []

    def generate_daily_watchlist(
        self, universe: List[str] = NIFTY_100, limit: int = 10
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

            screener_config = config_manager.get_screener_config()
            weights = screener_config.get("weights", {})
            filters = screener_config.get("filters", {})
            min_volume = filters.get("min_volume", 100000)
            min_value_traded = filters.get("min_value_traded", 10000000)

            scored_stocks = []

            for symbol, data in quotes.items():
                if "last_price" not in data or "ohlc" not in data:
                    continue

                ltp = data["last_price"]
                open_price = data["ohlc"]["open"]
                high_price = data["ohlc"]["high"]
                low_price = data["ohlc"]["low"]
                prev_close = data["ohlc"]["close"]
                volume = data.get("volume", 0)
                average_price = data.get("average_price", ltp)
                buy_quantity = data.get("buy_quantity", 0)
                sell_quantity = data.get("sell_quantity", 0)

                # We need some movement to trade. Avoid flat stocks.
                if prev_close == 0 or open_price == 0:
                    continue

                value_traded = volume * average_price
                if volume < min_volume or value_traded < min_value_traded:
                    continue

                # 1. Gap Percentage (Overnight movement)
                gap_pct = abs((open_price - prev_close) / prev_close) * 100

                # 2. Intraday Movement (Open to LTP)
                intraday_pct = abs((ltp - open_price) / open_price) * 100

                # 3. Volatility / Range
                volatility_pct = (
                    ((high_price - low_price) / open_price) * 100
                    if open_price > 0
                    else 0
                )

                # 4. Trend Strength
                trend_strength = (
                    abs(ltp - open_price) / (high_price - low_price)
                    if high_price > low_price
                    else 0
                )

                # 5. Liquidity
                liquidity = buy_quantity + sell_quantity

                # Baseline Score
                baseline_score = (intraday_pct * 2.0) + gap_pct

                clean_symbol = symbol.replace("NSE:", "")

                scored_stocks.append(
                    {
                        "symbol": clean_symbol,
                        "raw_baseline": baseline_score,
                        "raw_gap": gap_pct,
                        "raw_volatility": volatility_pct,
                        "raw_trend": trend_strength,
                        "raw_volume": volume,
                        "raw_liquidity": liquidity,
                        "volume": volume,
                        "intraday_pct": intraday_pct,
                    }
                )

            if not scored_stocks:
                # All candidates failed the liquidity/volume filters.
                # Return empty rather than letting the rejected instruments through.
                return []

            # Normalization (Min-Max Scaling)
            def normalize(items, key):
                values = [item[key] for item in items]
                min_val = min(values)
                max_val = max(values)
                range_val = max_val - min_val
                if range_val == 0:
                    for item in items:
                        item[f"norm_{key.replace('raw_', '')}"] = 0.5
                else:
                    for item in items:
                        item[f"norm_{key.replace('raw_', '')}"] = (
                            item[key] - min_val
                        ) / range_val

            normalize(scored_stocks, "raw_baseline")
            normalize(scored_stocks, "raw_gap")
            normalize(scored_stocks, "raw_volatility")
            normalize(scored_stocks, "raw_trend")
            normalize(scored_stocks, "raw_volume")
            normalize(scored_stocks, "raw_liquidity")

            w_baseline = weights.get("baseline", 1.0)
            w_gap = weights.get("gap", 1.0)
            w_volatility = weights.get("volatility", 1.0)
            w_trend = weights.get("trend", 1.0)
            w_volume = weights.get("volume", 1.0)
            w_liquidity = weights.get("liquidity", 1.0)

            for stock in scored_stocks:
                stock["score"] = (
                    stock["norm_baseline"] * w_baseline
                    + stock["norm_gap"] * w_gap
                    + stock["norm_volatility"] * w_volatility
                    + stock["norm_trend"] * w_trend
                    + stock["norm_volume"] * w_volume
                    + stock["norm_liquidity"] * w_liquidity
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
