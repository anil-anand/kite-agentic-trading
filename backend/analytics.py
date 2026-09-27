import csv
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .exit_management.models import ExitReasonCode
from .exit_quality import build_exit_quality_report, calculate_exit_quality
from .financial_eligibility import verified_outcome, verified_outcome_sql
from .journal import TradeJournal
from .llm_client import OpenAICompatibleClient
from .replay import replay_recorded_exit_decision
from .time_utils import EXCHANGE_TIMEZONE, as_utc


def _mapping(value: Any) -> Mapping[str, Any]:
    """Malformed retained subdocuments stay unavailable in read-only views."""
    return value if isinstance(value, Mapping) else {}


def _decision_trace(payload: Any) -> Mapping[str, Any]:
    return _mapping(_mapping(payload).get("trace"))


def _exchange_hour(value: Any) -> str:
    try:
        at = as_utc(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return at.astimezone(EXCHANGE_TIMEZONE).strftime("%H:00") if at else "UNKNOWN"


class TradeAnalytics:
    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path.home() / ".kite-agentic-trading" / "journal.db"
        else:
            self.db_path = Path(db_path)
        self._journal = None

    def _exit_journal(self) -> TradeJournal:
        # Reuse its thread-local connections; polling must not rerun schema
        # migrations and financial repairs on every operator refresh.
        if self._journal is None:
            self._journal = TradeJournal(str(self.db_path))
        return self._journal

    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _verified_clause(conn) -> str:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        return verified_outcome_sql(columns)

    def get_strategy_expectancy(self) -> List[Dict[str, Any]]:
        """
        Per-strategy expectancy: win rate, avg R, avg hold time, profit factor.
        """
        conn = self._get_conn()
        query = f"SELECT * FROM trades WHERE status = 'CLOSED' AND {self._verified_clause(conn)}"
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

            pnl = (
                r["net_pnl"]
                if "net_pnl" in r.keys() and r["net_pnl"] is not None
                else (r["pnl"] or 0.0)
            )

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
                    exit_t = as_utc(r["exit_time"])
                    entry_t = as_utc(r["entry_time"])
                    hold_time = (exit_t - entry_t).total_seconds() / 60.0
                    strategies[strat]["hold_times"].append(hold_time)
                except (TypeError, ValueError):
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
        query = f"SELECT * FROM trades WHERE status = 'CLOSED' AND {self._verified_clause(conn)}"
        rows = conn.execute(query).fetchall()

        confluence_stats = {}
        for r in rows:
            snapshot_str = r["confluence_snapshot"]
            direction = r["direction"]
            count = 1
            if snapshot_str:
                try:
                    snapshot = json.loads(snapshot_str)
                    if not isinstance(snapshot, dict):
                        raise ValueError("Snapshot is not a dict")

                    if "strategies" in snapshot:
                        if not isinstance(snapshot["strategies"], list):
                            raise ValueError("strategies is not a list")
                        # Explicitly count strategies matching trade direction
                        count = max(
                            1,
                            sum(
                                1
                                for s in snapshot["strategies"]
                                if s.get("direction") == direction
                            ),
                        )
                    else:
                        metadata_keys = {
                            "regime",
                            "regime_features",
                            "buy_signals",
                            "sell_signals",
                        }
                        if set(snapshot.keys()).intersection(metadata_keys):
                            raise ValueError("Malformed snapshot")
                        count = max(1, len(snapshot))
                except Exception:
                    count = "invalid"

            if count not in confluence_stats:
                confluence_stats[count] = {"trades": 0, "wins": 0, "pnl": 0.0}

            confluence_stats[count]["trades"] += 1
            pnl = (
                r["net_pnl"]
                if "net_pnl" in r.keys() and r["net_pnl"] is not None
                else (r["pnl"] or 0.0)
            )
            if pnl > 0:
                confluence_stats[count]["wins"] += 1
            confluence_stats[count]["pnl"] += pnl

        results = []

        def sort_key(item):
            k = item[0]
            return (1, k) if k == "invalid" else (0, k)

        for count, stats in sorted(confluence_stats.items(), key=sort_key):
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

    def get_signal_score_calibration(self) -> List[Dict[str, Any]]:
        """
        Bucket signals by signal score (e.g., 0-10, 10-20...) and compare with actual win rate (R-multiple >= 0.9).
        """
        conn = self._get_conn()
        query = (
            f"SELECT * FROM trades WHERE status = 'CLOSED' AND confidence IS NOT NULL "
            f"AND {self._verified_clause(conn)}"
        )
        rows = conn.execute(query).fetchall()

        buckets = {}
        for r in rows:
            conf = r["confidence"]
            bucket = (conf // 10) * 10

            if bucket not in buckets:
                buckets[bucket] = {"trades": 0, "wins": 0}

            # Only count valid trades
            if not r["entry_price"] or not r["stop_loss"] or not r["exit_price"]:
                continue

            risk = abs(r["entry_price"] - r["stop_loss"])
            if risk == 0:
                continue

            buckets[bucket]["trades"] += 1

            pnl_per_share = (
                (r["exit_price"] - r["entry_price"])
                if r["direction"] == "BUY"
                else (r["entry_price"] - r["exit_price"])
            )
            r_multiple = pnl_per_share / risk

            if r_multiple >= 0.9:
                buckets[bucket]["wins"] += 1

        results = []
        for b, stats in sorted(buckets.items()):
            win_rate = (
                (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0
            )
            results.append(
                {
                    "signal_score_bucket": f"{b}-{b + 9}",
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
            f"SELECT * FROM trades WHERE status = 'CLOSED' AND exit_reason IS NOT NULL "
            f"AND {self._verified_clause(conn)}"
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

    def _exit_quality_record(
        self, journal: TradeJournal, trade: Mapping[str, Any], *, decisions_out=None
    ) -> Dict[str, Any]:
        """Build one quality record from verified financial and phase-7 facts."""

        trade_id = str(trade.get("id") or "")
        managed = journal.get_managed_position_for_trade(trade_id)
        checkpoint = (managed or {}).get("state") or {}
        counters = checkpoint.get("counters") or {}
        extrema = checkpoint.get("extrema") or {}
        management = counters.get("exit_policy") or {}
        intents = (
            journal.get_position_order_intents(managed["position_key"])
            if managed is not None
            else []
        )
        decisions = (
            journal.get_exit_decisions(managed["position_key"])
            if managed is not None
            else []
        )
        if decisions_out is not None:
            decisions_out.extend(_mapping(row.get("payload")) for row in decisions)
        thesis = (
            (journal.get_position_thesis(managed["position_key"]) or {}).get("payload")
            if managed
            else None
        ) or {}
        binding = thesis.get("fill_binding") or {}
        reductions = [
            intent
            for intent in intents
            if intent.get("intent_type") in {"EXIT", "FLATTEN"}
        ]
        linked_id = (checkpoint.get("state") or {}).get("latched_exit_intent_id") or (
            checkpoint.get("intents") or {}
        ).get("exit_intent_id")
        reduction = next(
            (intent for intent in reductions if intent["intent_id"] == linked_id), None
        )
        fills = (
            journal.get_position_fills(
                managed["position_key"],
                broker_order_ids=journal.get_position_order_ids(
                    managed["position_key"]
                ),
            )
            if managed
            else []
        )
        if reduction is None:
            filled_intents = {fill.get("intent_id") for fill in fills}
            filled_reductions = [
                intent for intent in reductions if intent["intent_id"] in filled_intents
            ]
            if len(filled_reductions) == 1:
                reduction = filled_reductions[0]
        fill_at = None
        if reduction is not None:
            timed = []
            for fill in fills:
                if fill.get("intent_id") != reduction["intent_id"]:
                    continue
                try:
                    at = as_utc(fill.get("exchange_time"))
                except (TypeError, ValueError):
                    at = None
                if at is not None:
                    timed.append(at)
            fill_at = max(timed) if timed else None
        financial_quality = str(trade.get("financial_quality") or "UNAVAILABLE")
        # A shadow recommendation never initiated a real reduction. Only an
        # explicit retained intent join can establish decision-to-send latency.
        initiating = next(
            (
                item["payload"]
                for item in decisions
                if isinstance(item.get("payload"), Mapping)
                and _mapping(_decision_trace(item["payload"]).get("intent")).get(
                    "intent_id"
                )
                == (reduction or {}).get("intent_id")
                and reduction is not None
                and _mapping(_decision_trace(item["payload"]).get("orchestration")).get(
                    "dispatch"
                )
                != "SUPPRESSED_PHASE7"
            ),
            {},
        )
        raw_reason = (reduction or {}).get("reason")
        reason = (
            raw_reason
            if raw_reason in {code.value for code in ExitReasonCode}
            else None
        )
        exposure_path = self._retained_exposure_path(
            journal, managed, thesis, decisions
        )
        result = calculate_exit_quality(
            direction=str(trade.get("direction") or ""),
            entry_price=binding.get("entry_vwap"),
            initial_stop=thesis.get("initial_stop") if binding else None,
            initial_quantity=binding.get("filled_quantity"),
            realized_gross=trade.get("gross_pnl"),
            realized_net=trade.get("net_pnl"),
            exposure_path=exposure_path,
            observed_mfe_r=management.get(
                "observed_mfe_r", extrema.get("observed_mfe_r")
            ),
            observed_mae_r=management.get(
                "observed_mae_r", extrema.get("observed_mae_r")
            ),
            entry_at=trade.get("entry_time"),
            decision_at=initiating.get("occurred_at"),
            intent_at=(reduction or {}).get("created_at"),
            fill_at=fill_at,
            exit_at=trade.get("exit_time"),
            completed_bars=management.get("eligible_completed_bars"),
            reason_code=reason,
            execution_outcome_code=trade.get("exit_reason"),
            quality=financial_quality,
        )
        if not verified_outcome(trade):
            result["eligible"] = False
            result["exclusion_reason"] = "UNVERIFIED_FINANCIAL_OUTCOME"
        if not binding:
            result["eligible"] = False
            result["exclusion_reason"] = "IMMUTABLE_INITIAL_RISK_UNAVAILABLE"
        result["coverage"]["exposure_source"] = "RETAINED_DECISION_MARKS"
        retained = [
            item
            for item in decisions
            if isinstance(item.get("payload"), Mapping)
            and isinstance(
                _decision_trace(item["payload"]).get("input_snapshot"), Mapping
            )
        ]
        latest = _decision_trace(retained[-1]["payload"]) if retained else {}
        result.update(
            {
                "trade_id": trade_id,
                "symbol": trade.get("tradingsymbol"),
                "direction": trade.get("direction"),
                "playbook": thesis.get("playbook")
                or trade.get("production_playbook")
                or trade.get("strategy"),
                "setup_variant": thesis.get("setup_variant"),
                "regime": trade.get("market_regime")
                or _mapping(thesis.get("causal_anchors")).get("regime")
                or "UNKNOWN",
                "entry_regime": trade.get("market_regime")
                or _mapping(thesis.get("causal_anchors")).get("regime")
                or "UNKNOWN",
                "exit_regime": _mapping(latest.get("context")).get("confirmed_regime"),
                "regime_transition": _mapping(latest.get("context")).get(
                    "transition_candidate"
                ),
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time"),
                "entry_time_bucket": _exchange_hour(trade.get("entry_time")),
                "exit_time_bucket": _exchange_hour(trade.get("exit_time")),
                "liquidity": "UNKNOWN",
                "initiating_decision_id": initiating.get("decision_id"),
                "initiating_reason_text": raw_reason,
                "attribution_quality": "INTENT_LINKED" if reason else "UNKNOWN",
                "position_key": (managed or {}).get("position_key"),
                "retained_input_available": bool(retained),
                "decision_count": len(decisions),
                "retained_input_count": len(retained),
                "action_distribution": dict(
                    Counter(
                        (item.get("payload") or {}).get("action") or "UNKNOWN"
                        for item in decisions
                    )
                ),
                "assessment_reason_distribution": dict(
                    Counter(
                        (item.get("payload") or {}).get("primary_reason_code")
                        or "UNKNOWN"
                        for item in decisions
                    )
                ),
            }
        )
        if managed is None:
            result["replay_status"] = "LEGACY_OR_UNMANAGED"
        elif managed.get("state_corrupt"):
            result["replay_status"] = "STATE_CORRUPT"
        elif retained:
            result["replay_status"] = "RETAINED_CANDIDATE_INPUTS"
        else:
            result["replay_status"] = "RETAINED_INPUTS_UNAVAILABLE"
        return result

    @staticmethod
    def _retained_exposure_path(journal, managed, thesis, decisions):
        """Join observed marks to allocated partial fills without inventing P&L.

        The retained broker quantity must agree with the timed fill ledger at
        each sample. Missing allocations or execution times exclude that sample;
        a current or hypothetical shadow residual never substitutes for a fact.
        """
        binding = thesis.get("fill_binding") or {}
        if managed is None or not binding:
            return []
        entry, quantity = binding["entry_vwap"], binding["filled_quantity"]
        sign = 1 if thesis["direction"] == "BUY" else -1
        terminal_at = as_utc(binding.get("entry_terminal_at"))
        if terminal_at is None:
            return []
        fills = journal.get_position_fills(
            managed["position_key"],
            broker_order_ids=journal.get_position_order_ids(managed["position_key"]),
        )
        reductions = []
        for fill in fills:
            if fill["side"] == thesis["direction"]:
                continue
            price = fill.get("fill_price")
            try:
                at = as_utc(fill.get("exchange_time"))
            except (TypeError, ValueError):
                return [{} for _ in decisions]
            if (
                at is None
                or not isinstance(price, (int, float))
                or not math.isfinite(price)
                or price <= 0
            ):
                return [{} for _ in decisions]
            reductions.append((at, fill["quantity"], price))
        path = []
        for row in decisions:
            record = row.get("payload") or {}
            trace = _decision_trace(record)
            actual = _mapping(
                _mapping(trace.get("orchestration")).get("actual_state_before")
            ) or _mapping(_mapping(trace.get("input_snapshot")).get("state"))
            mark = trace.get("mark_price")
            try:
                at = as_utc(record.get("occurred_at"))
            except (TypeError, ValueError):
                path.append({})
                continue
            if (
                at is None
                or at < terminal_at
                or not isinstance(mark, (int, float))
                or not math.isfinite(mark)
                or mark <= 0
            ):
                path.append({})
                continue
            filled = [item for item in reductions if item[0] <= at]
            residual = quantity - sum(item[1] for item in filled)
            if residual <= 0 or residual != actual.get("known_quantity"):
                path.append({})
                continue
            path.append(
                {
                    "timestamp": at.isoformat(),
                    "mark_price": mark,
                    "residual_quantity": residual,
                    "realized_gross": sum(
                        sign * size * (price - entry) for _, size, price in filled
                    ),
                }
            )
        return path

    def get_exit_quality_report(self) -> Dict[str, Any]:
        """Return aggregate metrics with exclusions and MTM limits made explicit."""

        journal = self._exit_journal()
        decisions = []
        records = [
            self._exit_quality_record(journal, trade, decisions_out=decisions)
            for trade in journal.get_trades()
        ]
        report = build_exit_quality_report(records, decisions=decisions)
        report["decision_coverage"]["retained_input_count"] = sum(
            record["retained_input_count"] for record in records
        )
        for name in ("action_distribution", "assessment_reason_distribution"):
            counts = Counter()
            for record in records:
                counts.update(record[name])
            report[name] = dict(counts)
        report["records"] = records
        report["research_label"] = (
            "JOURNAL_EXIT_QUALITY; journal rows lack a complete portfolio MTM "
            "series, so drawdown/session returns are unavailable here."
        )
        return report

    def get_exit_quality_for_trade(self, trade_id: str) -> Dict[str, Any]:
        """Return a one-trade metric record without fetching current market data."""

        journal = self._exit_journal()
        trade = journal.get_trade(trade_id)
        if trade is None:
            return {"error": "Trade not found"}
        result = self._exit_quality_record(journal, trade)
        # A journal decision trace captures as-of facts, not forward candles.
        # Until a phase-8 scenario artifact is attached, pretending to hold the
        # trade would recreate F19's unreachable-target error.
        result["hold_n"] = {
            "status": "CENSORED",
            "censor_reason": "FORWARD_RETAINED_PATH_UNAVAILABLE",
            "message": (
                "No forward retained-path artifact is attached to this trade; "
                "a risk-constrained hold-N result is therefore unavailable."
            ),
        }
        return result

    def get_exit_management_replay(self, trade_id: str) -> Dict[str, Any]:
        """Load and verify each retained candidate decision for operator replay."""

        journal = self._exit_journal()
        replay = journal.get_exit_management_replay(trade_id)
        if replay is None:
            return {
                "trade_id": trade_id,
                "available": False,
                "reason": "LEGACY_OR_UNMANAGED_TRADE",
                "decisions": [],
            }
        verification = []
        for record in replay["decisions"]:
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                verification.append(
                    {
                        "decision_id": record.get("decision_id"),
                        "status": "CORRUPT_RETAINED_PAYLOAD",
                    }
                )
                continue
            try:
                evaluation = replay_recorded_exit_decision(payload)
            except (
                AssertionError,
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                verification.append(
                    {
                        "decision_id": record.get("decision_id"),
                        "status": "REPLAY_MISMATCH",
                        "message": str(exc),
                    }
                )
            else:
                verification.append(
                    {
                        "decision_id": record.get("decision_id"),
                        "status": "REPRODUCED",
                        "action": evaluation.decision.action.value,
                        "reason_code": evaluation.decision.primary_reason_code,
                    }
                )
        replay["available"] = bool(verification) and not replay["position"].get(
            "state_corrupt"
        )
        replay["exact_replay_complete"] = (
            bool(verification)
            and all(item["status"] == "REPRODUCED" for item in verification)
            and not replay["position"].get("state_corrupt")
        )
        replay["verification"] = verification
        replay["replayability"] = {
            "reproduced": sum(item["status"] == "REPRODUCED" for item in verification),
            "total": len(verification),
            "uses_current_market_data": False,
        }
        return replay

    def get_active_position_explanations(self) -> List[Dict[str, Any]]:
        """Provide compact, factual active-position panels for the renderer."""

        journal = self._exit_journal()
        panels = []
        for position in journal.list_managed_positions(include_closed=False):
            checkpoint = position.get("state") or {}
            state = checkpoint.get("state") or {}
            thesis_record = journal.get_position_thesis(position["position_key"])
            thesis = (thesis_record or {}).get("payload") or {}
            latest_record = journal.get_latest_exit_decision(position["position_key"])
            latest = (latest_record or {}).get("payload")
            protection = checkpoint.get("protection") or {}
            counters = checkpoint.get("counters") or {}
            candidate = counters.get("shadow_candidate_position_state") or state
            memory = counters.get("exit_policy") or {}
            trace = _decision_trace(latest)
            context = _mapping(trace.get("context"))
            profit = _mapping(trace.get("profit_context"))
            decision_corrupt = bool(latest_record) and (
                bool(latest_record.get("payload_corrupt"))
                or not isinstance(_mapping(latest).get("trace"), Mapping)
                or any(
                    trace.get(key) is not None and not isinstance(trace[key], Mapping)
                    for key in (
                        "context",
                        "risk",
                        "profit_context",
                        "input_snapshot",
                        "orchestration",
                    )
                )
            )
            profile = thesis.get("management_profile") or {}
            profile_values = profile.get("values") or {}
            boundary = profile_values.get("entry_boundary") or {}
            boundary_price = boundary.get("price")
            if boundary_price is None:
                upper = (thesis.get("direction") == "BUY") == (
                    profile.get("name") == "breakout_follow_through"
                )
                boundary_price = boundary.get("high" if upper else "low")
            pending_id = state.get("latched_exit_intent_id") or (
                checkpoint.get("intents") or {}
            ).get("protection_intent_id")
            pending = (
                journal.get_order_intent_projection(pending_id) if pending_id else None
            )
            if pending is not None and not pending.get("active"):
                pending = None
            expected = {
                "trend_continuation": "Hold controlled pullbacks while the defended structure remains valid.",
                "breakout_follow_through": "Allow a retest; exit on confirmed acceptance back inside the entry range.",
                "range_convergence": "Converge toward the pinned objective while the original range remains valid.",
                "unknown_legacy_bounded": "Original premise unavailable; retain confirmed protection and pinned objective/session controls.",
            }
            panels.append(
                {
                    "position_key": position["position_key"],
                    "broker_position_key": ":".join(
                        str(position[key])
                        for key in (
                            "namespace",
                            "account_id",
                            "exchange",
                            "instrument_id",
                            "tradingsymbol",
                            "product",
                        )
                    )
                    if all(
                        position.get(key) is not None
                        for key in (
                            "namespace",
                            "account_id",
                            "exchange",
                            "instrument_id",
                            "tradingsymbol",
                            "product",
                        )
                    )
                    else None,
                    "symbol": position.get("tradingsymbol"),
                    **{
                        key: position.get(key)
                        for key in (
                            "namespace",
                            "account_id",
                            "exchange",
                            "product",
                            "instrument_id",
                        )
                    },
                    "direction": thesis.get("direction"),
                    "policy_mode": counters.get("exit_policy_mode"),
                    "residual_quantity": state.get("known_quantity"),
                    "pending_intent": {
                        "intent_id": pending["intent_id"],
                        "intent_type": pending["intent_type"],
                        "status": pending["state"],
                        "quantity": pending["residual_quantity"],
                        "reason": pending.get("reason"),
                    }
                    if pending
                    else None,
                    "state_corrupt": position.get("state_corrupt", False),
                    "thesis": {
                        "strategy": thesis.get("strategy"),
                        "playbook": thesis.get("playbook"),
                        "setup_variant": thesis.get("setup_variant"),
                        "reasoning": thesis.get("reasoning"),
                        "expected_behavior": profile_values.get("expected_behavior")
                        or expected.get(profile.get("name")),
                        "original_boundary": boundary_price,
                        "boundary_details": boundary,
                        "initial_stop": thesis.get("initial_stop"),
                        "entry_price": (thesis.get("fill_binding") or {}).get(
                            "entry_vwap"
                        ),
                    },
                    "health": candidate.get("thesis_health"),
                    "development": candidate.get("development"),
                    "exposure": state.get("exposure"),
                    "protection": {
                        "quality": state.get("protection"),
                        "confirmed_stop": protection.get("confirmed_stop"),
                        "requested_stop": protection.get("requested_stop"),
                        "protected_quantity": protection.get("protected_quantity"),
                        "confirmed_stop_order_id": protection.get(
                            "confirmed_stop_order_id"
                        ),
                    },
                    "context": context,
                    "quality": {
                        "assessment_at": (latest or {}).get("occurred_at"),
                        "checkpoint_at": position.get("updated_at"),
                        "primary": context.get("primary_quality"),
                        "higher": context.get("higher_quality"),
                        "primary_issues": context.get("primary_issues", []),
                        "higher_issues": context.get("higher_issues", []),
                        "decision_corrupt": decision_corrupt,
                        "broker_state_known": _mapping(trace.get("risk")).get(
                            "broker_state_known"
                        ),
                        "daily_loss_latched": _mapping(trace.get("risk")).get(
                            "daily_loss_latched"
                        ),
                    },
                    "management": {
                        "mfe_r": memory.get("observed_mfe_r"),
                        "mae_r": memory.get("observed_mae_r"),
                        "u_r": profit.get("unrealized_r"),
                        "giveback_r": profit.get("giveback_r"),
                        "session_remaining_minutes": None,
                        "last_bar_end": counters.get("exit_shadow_last_bar_end"),
                    },
                    "latest_decision": latest,
                }
            )
        return panels

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
            entry_time = as_utc(entry_time_str)
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

        formatted_candles = []
        for c in candles:
            # kiteconnect returns 'date' as a datetime object or string
            dt = c["date"]
            if isinstance(dt, str):
                # Preserve the ISO offset — stripping it would produce a naive
                # datetime that timestamp() interprets in the host timezone,
                # shifting all candles by 5.5 h on a UTC server.
                dt = as_utc(dt)

            formatted_candles.append(
                {
                    "time": int(dt.timestamp()),
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "volume": c.get("volume", 0),
                }
            )

        return {"trade": dict(trade), "candles": formatted_candles}

    def get_what_if_analysis(self, trade_id: str) -> Dict[str, Any]:
        """Compatibility endpoint: never resurrect unconstrained legacy what-ifs.

        Corrected vendor history cannot establish retained stops, account risk,
        or the original as-of path. Consumers receive explicit unavailability
        until a retained research artifact can support a constrained replay.
        """
        return {
            "error": "Legacy unconstrained what-if retired; use retained exit-quality replay.",
            "status": "UNAVAILABLE",
            "trade_id": trade_id,
            "censor_reason": "FORWARD_RETAINED_PATH_UNAVAILABLE",
        }

    def generate_llm_post_mortem(self, trade_id: str) -> Dict[str, Any]:
        """
        Uses the configured OpenAI-compatible provider to generate a post-mortem.
        """
        from .config import config_manager

        creds = config_manager.get_credentials()
        llm = config_manager.get_llm_settings()
        api_key = creds.get("llmApiKey")
        provider = llm.get("provider", "Gemini")
        if not api_key and provider != "Ollama":
            return {"error": "LLM API Key not configured in settings."}

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
            analysis = OpenAICompatibleClient().generate(
                base_url=llm.get("baseUrl", ""),
                api_key=api_key,
                model=llm.get("model", ""),
                prompt=prompt,
                provider=provider,
                plan=llm.get("openCodePlan", "zen"),
            )
            return {"analysis": analysis}
        except Exception as e:
            return {"error": f"LLM Generation failed: {str(e)}"}


analytics = TradeAnalytics()
