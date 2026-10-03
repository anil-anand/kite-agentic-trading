import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from .dev_mode import runtime_data_dir
from .financial_eligibility import verified_outcome_sql


def bucket_success_probability(rows) -> Tuple[Optional[float], int]:
    """Pure legacy gross +0.9R bucket estimate for already eligible outcomes.

    Eligibility and causal score-bucket selection belong to the adapter. This
    preserves the live minimum count and denominator, including zero-risk rows.
    It is descriptive outcome frequency, not calibrated exit-failure probability.
    """
    rows = list(rows)
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


class ProbabilityCalibrator:
    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = runtime_data_dir() / "journal.db"
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
        Returns descriptive (success_frequency, sample_size) for eligible closed trades.
        Requires at least 10 trades in the bucket to return a valid probability.
        Success is defined as trade exiting with at least +0.9R profit.
        """
        conn = self._get_conn()
        try:
            # We bucket the score in groups of 10 for adequate sample sizes
            bucket_min = (signal_score // 10) * 10
            bucket_max = bucket_min + 9

            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
            }
            query = """
                SELECT direction, entry_price, target, stop_loss, exit_price
                FROM trades
                WHERE {eligible}
                  AND strategy = ?
                  AND confidence >= ? AND confidence <= ?
            """.format(eligible=verified_outcome_sql(columns))
            rows = conn.execute(query, (strategy, bucket_min, bucket_max)).fetchall()

            return bucket_success_probability(rows)
        finally:
            conn.close()


calibrator = ProbabilityCalibrator()
