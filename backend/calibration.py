import sqlite3
from pathlib import Path
from typing import Optional, Tuple


class ProbabilityCalibrator:
    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path.home() / ".kite-agentic-trading" / "journal.db"
        else:
            self.db_path = Path(db_path)

    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_probability(
        self, strategy: str, signal_score: int
    ) -> Tuple[Optional[float], int]:
        """
        Returns (estimated_probability, sample_size) based on out-of-sample historical trades.
        Requires at least 10 trades in the bucket to return a valid probability.
        Success is defined as trade exiting with at least +0.9R profit.
        """
        conn = self._get_conn()
        try:
            # We bucket the score in groups of 10 for adequate sample sizes
            bucket_min = (signal_score // 10) * 10
            bucket_max = bucket_min + 9

            query = """
                SELECT direction, entry_price, target, stop_loss, exit_price
                FROM trades
                WHERE status = 'CLOSED'
                  AND strategy = ?
                  AND confidence >= ? AND confidence <= ?
            """
            rows = conn.execute(query, (strategy, bucket_min, bucket_max)).fetchall()

            sample_size = len(rows)
            if sample_size < 10:
                return None, sample_size

            successes = 0
            for r in rows:
                if not r["entry_price"] or not r["stop_loss"] or not r["exit_price"]:
                    continue

                risk = abs(r["entry_price"] - r["stop_loss"])
                if risk == 0:
                    continue

                pnl_per_share = (
                    (r["exit_price"] - r["entry_price"])
                    if r["direction"] == "BUY"
                    else (r["entry_price"] - r["exit_price"])
                )
                r_multiple = pnl_per_share / risk

                # Reached roughly +1R target (allowing for some slippage)
                if r_multiple >= 0.9:
                    successes += 1

            prob = successes / sample_size
            return prob, sample_size
        finally:
            conn.close()


calibrator = ProbabilityCalibrator()
