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


analytics = TradeAnalytics()
