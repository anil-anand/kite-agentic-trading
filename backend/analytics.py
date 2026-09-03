import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


class TradeAnalytics:
    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path.home() / ".kite-agentic-trading" / "journal.db"
        else:
            self.db_path = Path(db_path)

    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_strategy_expectancy(self) -> List[Dict[str, Any]]:
        """
        Per-strategy expectancy: win rate, avg R, avg hold time, profit factor.
        """
        conn = self._get_conn()
        query = "SELECT * FROM trades WHERE status = 'CLOSED'"
        rows = conn.execute(query).fetchall()

        strategies = {}
        for r in rows:
            strat = r["strategy"]
            if strat not in strategies:
                strategies[strat] = {
                    "trades": 0,
                    "wins": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "r_multiples": [],
                    "hold_times": [],
                }

            strategies[strat]["trades"] += 1
            pnl = r["pnl"] or 0.0
            if pnl > 0:
                strategies[strat]["wins"] += 1
                strategies[strat]["gross_profit"] += pnl
            else:
                strategies[strat]["gross_loss"] += abs(pnl)

            # R multiple
            risk = (
                abs(r["entry_price"] - r["stop_loss"])
                if r["entry_price"] and r["stop_loss"]
                else 0
            )
            if risk > 0:
                pnl_per_share = (
                    (r["exit_price"] - r["entry_price"])
                    if r["direction"] == "BUY"
                    else (r["entry_price"] - r["exit_price"])
                )
                r_multiple = pnl_per_share / risk
                strategies[strat]["r_multiples"].append(r_multiple)

            # Hold time
            if r["exit_time"] and r["entry_time"]:
                try:
                    exit_t = datetime.fromisoformat(r["exit_time"])
                    entry_t = datetime.fromisoformat(r["entry_time"])
                    hold_time = (exit_t - entry_t).total_seconds() / 60.0
                    strategies[strat]["hold_times"].append(hold_time)
                except ValueError:
                    pass

        results = []
        for strat, stats in strategies.items():
            win_rate = (
                (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0
            )
            profit_factor = (
                (stats["gross_profit"] / stats["gross_loss"])
                if stats["gross_loss"] > 0
                else float("inf")
            )
            avg_r = (
                sum(stats["r_multiples"]) / len(stats["r_multiples"])
                if stats["r_multiples"]
                else 0
            )
            avg_hold = (
                sum(stats["hold_times"]) / len(stats["hold_times"])
                if stats["hold_times"]
                else 0
            )

            results.append(
                {
                    "strategy": strat,
                    "total_trades": stats["trades"],
                    "win_rate_pct": round(win_rate, 2),
                    "profit_factor": round(profit_factor, 2)
                    if profit_factor != float("inf")
                    else None,
                    "avg_r_multiple": round(avg_r, 2),
                    "avg_hold_time_mins": round(avg_hold, 2),
                }
            )

        return results

    def get_confluence_validation(self) -> List[Dict[str, Any]]:
        """
        Confluence validation: win rate and profit by number of firing strategies at entry.
        """
        conn = self._get_conn()
        query = "SELECT * FROM trades WHERE status = 'CLOSED'"
        rows = conn.execute(query).fetchall()

        confluence_stats = {}
        for r in rows:
            snapshot_str = r["confluence_snapshot"]
            count = 1
            if snapshot_str:
                try:
                    snapshot = json.loads(snapshot_str)
                    if isinstance(snapshot, dict):
                        count = max(1, len(snapshot))
                except Exception:
                    pass

            if count not in confluence_stats:
                confluence_stats[count] = {"trades": 0, "wins": 0, "pnl": 0.0}

            confluence_stats[count]["trades"] += 1
            pnl = r["pnl"] or 0.0
            if pnl > 0:
                confluence_stats[count]["wins"] += 1
            confluence_stats[count]["pnl"] += pnl

        results = []
        for count, stats in sorted(confluence_stats.items()):
            win_rate = (
                (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0
            )
            results.append(
                {
                    "confluence_count": count,
                    "total_trades": stats["trades"],
                    "win_rate_pct": round(win_rate, 2),
                    "total_pnl": round(stats["pnl"], 2),
                }
            )

        return results

    def get_confidence_calibration(self) -> List[Dict[str, Any]]:
        """
        Bucket signals by confidence (e.g., 0-10, 10-20...) and compare with actual win rate.
        """
        conn = self._get_conn()
        query = (
            "SELECT * FROM trades WHERE status = 'CLOSED' AND confidence IS NOT NULL"
        )
        rows = conn.execute(query).fetchall()

        buckets = {}
        for r in rows:
            conf = r["confidence"]
            bucket = (conf // 10) * 10

            if bucket not in buckets:
                buckets[bucket] = {"trades": 0, "wins": 0}

            buckets[bucket]["trades"] += 1
            if (r["pnl"] or 0.0) > 0:
                buckets[bucket]["wins"] += 1

        results = []
        for b, stats in sorted(buckets.items()):
            win_rate = (
                (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0
            )
            results.append(
                {
                    "confidence_bucket": f"{b}-{b + 9}",
                    "total_trades": stats["trades"],
                    "actual_win_rate_pct": round(win_rate, 2),
                }
            )

        return results

    def get_exit_reason_effectiveness(self) -> List[Dict[str, Any]]:
        """
        Effectiveness by exit reason.
        """
        conn = self._get_conn()
        query = (
            "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_reason IS NOT NULL"
        )
        rows = conn.execute(query).fetchall()

        reasons = {}
        for r in rows:
            reason = r["exit_reason"]
            if reason not in reasons:
                reasons[reason] = {"trades": 0, "wins": 0, "pnl": 0.0}

            reasons[reason]["trades"] += 1
            pnl = r["pnl"] or 0.0
            if pnl > 0:
                reasons[reason]["wins"] += 1
            reasons[reason]["pnl"] += pnl

        results = []
        for reason, stats in reasons.items():
            win_rate = (
                (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0
            )
            results.append(
                {
                    "exit_reason": reason,
                    "total_trades": stats["trades"],
                    "win_rate_pct": round(win_rate, 2),
                    "total_pnl": round(stats["pnl"], 2),
                }
            )

        return sorted(results, key=lambda x: x["total_pnl"], reverse=True)

    def export_to_csv(self, filepath: str) -> None:
        """
        Export all trades to a CSV file.
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM trades ORDER BY entry_time DESC")
        rows = cursor.fetchall()

        if not rows:
            return

        columns = [description[0] for description in cursor.description]

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(row)

    def get_trade_replay(self, trade_id: str) -> Dict[str, Any]:
        """
        Fetches historical minute-level candle data around the trade's timeframe
        and returns it for frontend charting.
        """
        from .kite_client import kite_client

        conn = self._get_conn()
        query = "SELECT * FROM trades WHERE id = ?"
        trade = conn.execute(query, (trade_id,)).fetchone()

        if not trade:
            return {"error": "Trade not found"}

        tradingsymbol = trade["tradingsymbol"]
        entry_time_str = trade["entry_time"]

        if not entry_time_str:
            return {"error": "Trade has no entry time"}

        try:
            entry_time = datetime.fromisoformat(entry_time_str)
            # Fetch data for the whole day of the trade
            from_date = entry_time.strftime("%Y-%m-%d 09:15:00")
            to_date = entry_time.strftime("%Y-%m-%d 15:30:00")
        except ValueError:
            return {"error": "Invalid entry time format"}

        # Get instrument token
        instruments = kite_client.get_instruments(trade["exchange"] or "NSE")
        instrument_token = next(
            (
                i["instrument_token"]
                for i in instruments
                if i["tradingsymbol"] == tradingsymbol
            ),
            None,
        )

        if not instrument_token:
            return {"error": f"Could not find instrument token for {tradingsymbol}"}

        # Fetch historical data (1 minute interval)
        try:
            candles = kite_client.get_historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval="minute",
            )
        except Exception as e:
            return {"error": f"Failed to fetch historical data: {str(e)}"}

        return {"trade": dict(trade), "candles": candles}

    def get_what_if_analysis(self, trade_id: str) -> Dict[str, Any]:
        """
        Simulates alternative exit scenarios based on historical data.
        """
        replay_data = self.get_trade_replay(trade_id)
        if "error" in replay_data:
            return replay_data

        trade = replay_data["trade"]
        candles = replay_data["candles"]

        if not candles:
            return {"error": "No historical data available"}

        entry_price = trade["entry_price"]
        direction = trade["direction"]
        quantity = trade["quantity"]
        target = trade["target"]
        stop_loss = trade["stop_loss"]

        try:
            entry_time = datetime.fromisoformat(trade["entry_time"])
        except Exception:
            return {"error": "Invalid entry time"}

        # Filter candles to only those after entry
        post_entry_candles = []
        for c in candles:
            # c["date"] is usually a datetime object from kiteconnect
            candle_time = (
                c["date"]
                if isinstance(c["date"], datetime)
                else datetime.fromisoformat(str(c["date"]).replace("+05:30", ""))
            )
            if candle_time.timestamp() >= entry_time.timestamp():
                post_entry_candles.append(c)

        # 1. Hold to EOD (last candle of the day)
        eod_pnl = 0
        eod_price = (
            post_entry_candles[-1]["close"] if post_entry_candles else entry_price
        )
        if direction == "BUY":
            eod_pnl = (eod_price - entry_price) * quantity
        else:
            eod_pnl = (entry_price - eod_price) * quantity

        # 2. Target Hit
        target_hit = False
        target_hit_time = None
        for c in post_entry_candles:
            if direction == "BUY" and c["high"] >= target:
                target_hit = True
                target_hit_time = str(c["date"])
                break
            elif direction == "SELL" and c["low"] <= target:
                target_hit = True
                target_hit_time = str(c["date"])
                break

        # 3. Wider Stop (1.5x)
        original_risk = abs(entry_price - stop_loss)
        wider_stop = (
            entry_price - (original_risk * 1.5)
            if direction == "BUY"
            else entry_price + (original_risk * 1.5)
        )
        wider_stop_hit = False
        wider_stop_pnl = 0
        for c in post_entry_candles:
            if direction == "BUY" and c["low"] <= wider_stop:
                wider_stop_hit = True
                wider_stop_pnl = (wider_stop - entry_price) * quantity
                break
            elif direction == "SELL" and c["high"] >= wider_stop:
                wider_stop_hit = True
                wider_stop_pnl = (entry_price - wider_stop) * quantity
                break

        if not wider_stop_hit:
            wider_stop_pnl = eod_pnl

        return {
            "eod_pnl": eod_pnl,
            "target_hit": target_hit,
            "target_hit_time": target_hit_time,
            "wider_stop_price": wider_stop,
            "wider_stop_hit": wider_stop_hit,
            "wider_stop_pnl": wider_stop_pnl,
            "actual_pnl": trade["pnl"],
        }

    def generate_llm_post_mortem(self, trade_id: str) -> Dict[str, Any]:
        """
        Uses google-genai to generate a post-mortem analysis of the trade.
        """
        from .config import config_manager

        creds = config_manager.get_credentials()
        api_key = creds.get("llmApiKey")
        if not api_key:
            return {"error": "LLM API Key not configured in settings."}

        try:
            from google import genai
        except ImportError:
            return {"error": "google-genai package not installed."}

        conn = self._get_conn()
        trade = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not trade:
            return {"error": "Trade not found"}

        events = conn.execute(
            "SELECT * FROM trade_events WHERE trade_id = ? ORDER BY timestamp ASC",
            (trade_id,),
        ).fetchall()

        trade_dict = dict(trade)
        events_list = [dict(e) for e in events]

        prompt = f"""
Analyze this intraday trade from a systematic trading algorithm and provide a short, insightful post-mortem.
Focus on:
1. Why we likely entered based on the strategy and confluence snapshot.
2. What happened during the trade lifecycle (events).
3. Why the exit happened and if it was optimal based on the MAE/MFE (if deducible) and exit reason.
4. Key takeaway for future trades.

Trade Details:
- Symbol: {trade_dict.get("tradingsymbol")}
- Strategy: {trade_dict.get("strategy")}
- Direction: {trade_dict.get("direction")}
- Entry Time: {trade_dict.get("entry_time")}
- Exit Time: {trade_dict.get("exit_time")}
- Entry Price: {trade_dict.get("entry_price")}
- Exit Price: {trade_dict.get("exit_price")}
- Target: {trade_dict.get("target")}
- Stop Loss: {trade_dict.get("stop_loss")}
- PnL: {trade_dict.get("pnl")}
- Exit Reason: {trade_dict.get("exit_reason")}

Confluence Snapshot:
{trade_dict.get("confluence_snapshot")}

Indicator Snapshot:
{trade_dict.get("indicator_snapshot")}

Trade Timeline Events:
"""
        for event in events_list:
            prompt += f"- {event.get('timestamp')}: {event.get('event_type')} - {event.get('details')}\\n"

        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
            )
            return {"analysis": response.text}
        except Exception as e:
            return {"error": f"LLM Generation failed: {str(e)}"}


analytics = TradeAnalytics()
