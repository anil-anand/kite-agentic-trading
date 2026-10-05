"""Run isolated real-time paper management from authorized external JSONL input.

No data vendor, network client, credential store or live broker is reachable.
Entry signals and warmup history are supplied by the feed; this is an exit-policy
operational session, not evidence of production entry-selection parity.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import uuid
from datetime import timedelta

from ..entry_ordering import PRODUCTION_CANDLE_HISTORY_DAYS
from ..exit_management.models import (
    ExposureState,
    PositionState,
    ProtectionState,
    ThesisHealth,
)
from ..exit_management.thesis import (
    bind_terminal_fill,
    calculate_provisional_risk,
    capture_entry_thesis,
)
from ..journal import TradeJournal
from ..market_context import ContextPolicy, MarketContextService
from ..order_lifecycle import OrderLifecycleCoordinator
from .operational_capture import OperationalRecorder, _at, _isolated, _json
from .paper_broker import PaperBroker
from .research_study import (
    _causal_snapshot,
    restore_exit_policy,
    restore_session_policy,
)
from .simulated_broker import SimulationExecutionPolicy


class RealtimePaperSession:
    def __init__(self, directory, *, plan, fixture_clock=None):
        capture = {
            name: plan[name]
            for name in (
                "study_id",
                "mode",
                "data_source_id",
                "source_revision",
                "policy",
            )
        }
        for field in ("capture_slot", "capture_attempt_id"):
            if field in plan:
                capture[field] = plan[field]
        if capture["mode"] != "ISOLATED_PAPER":
            raise ValueError("paper sessions require ISOLATED_PAPER")
        self.recorder = OperationalRecorder(
            directory, plan=capture, fixture_clock=fixture_clock
        )
        self.policy = restore_exit_policy(plan["policy"])
        self.context_policy = ContextPolicy(**plan.get("context_policy", {}))
        self.session_policy = restore_session_policy(plan.get("session_policy"))
        self.context_service = MarketContextService(
            self.context_policy, self.session_policy
        )
        self.maximum_lag = float(plan.get("maximum_feed_lag_seconds", 30))
        if not 0 < self.maximum_lag <= 300:
            raise ValueError(
                "feed lag must be positive and no more than one primary bar"
            )
        self.broker = PaperBroker(
            initial_capital=plan.get("initial_capital", 100000),
            data_source_id=plan["data_source_id"],
            execution_policy=SimulationExecutionPolicy(
                **plan.get("execution_policy", {})
            ),
        )
        self.journal = TradeJournal(str(self.recorder.directory / "paper.sqlite"))
        self.runner = self.broker.create_candidate_runner(
            coordinator=OrderLifecycleCoordinator(self.journal),
            session_policy=self.session_policy,
            daily_loss_limit=plan.get("daily_loss_limit"),
        )
        self.history = {}
        self.fill_receipts = {}
        self._last_bar = {}
        self._candidate_links = {}
        self.decision_count = 0

    def _fresh(self, value, received):
        at = _at(value)
        if not 0 <= (received - at).total_seconds() <= self.maximum_lag:
            raise ValueError(
                "historical or future events are not real-time paper evidence"
            )
        return at

    def ingest(self, message, *, _received_at=None):
        try:
            received = (
                self.recorder.received() if _received_at is None else _at(_received_at)
            )
            message = _json(message)
            kind = message["type"]
            if kind == "history":
                symbol = message["symbol"]
                if symbol in self.runner.positions:
                    raise ValueError("warmup history must precede entry")
                rows = []
                for raw in message["candles"]:
                    row = dict(raw)
                    row["date"] = _at(row["date"])
                    if row["date"] + timedelta(minutes=5) > received:
                        raise ValueError("warmup contains an incomplete or future bar")
                    row["available_at"] = received
                    row["received_at"] = received
                    rows.append(row)
                self.history[symbol] = rows
            elif kind == "entry":
                self._entry(message, received)
            elif kind in {"candle", "clock", "quote"}:
                candles, contexts = {}, {}
                if kind == "quote":
                    observed = _at(message["observed_at"])
                    if observed > received:
                        raise ValueError("future quote cannot be paper evidence")
                    symbol = message["symbol"]
                    if symbol not in self.runner.positions:
                        raise ValueError("paper quote requires a registered position")
                    if observed >= self.broker._mark_times.get(symbol, observed):
                        self.broker.mark(symbol, message["price"], observed)
                    self.recorder.append(
                        "MARK_QUOTE",
                        {
                            "symbol": symbol,
                            "observed_at": observed.isoformat(),
                            "received_at": received.isoformat(),
                            "price": message["price"],
                            "execution_model": "MARK_ONLY_BAR_EXECUTION_UNCHANGED",
                        },
                    )
                if kind == "candle":
                    symbol = message["symbol"]
                    row = dict(message["candle"])
                    row["date"] = _at(row["date"])
                    self._fresh(row["date"] + timedelta(minutes=5), received)
                    if (
                        symbol in self._last_bar
                        and row["date"] <= self._last_bar[symbol]
                    ):
                        raise ValueError("paper bars must advance without revisions")
                    row["received_at"] = received
                    row["available_at"] = received
                    self._last_bar[symbol] = row["date"]
                    self.history.setdefault(symbol, []).append(row)
                    position = self.broker.positions.get(symbol)
                    if position and row["date"] < position["entry_time"]:
                        completed = row["date"] + timedelta(minutes=5)
                        if completed > position["entry_time"]:
                            position["excursion_quality"] = "PARTIAL_BAR_BOUNDS"
                        if completed >= self.broker._mark_times.get(symbol, completed):
                            self.broker.mark(symbol, row["close"], completed)
                    elif self.broker._mark_times.get(symbol, row["date"]) > row[
                        "date"
                    ] + timedelta(minutes=5):
                        # A quote/clock decision has already advanced past this
                        # executable interval. Retain newly available context,
                        # but never rewind fills, marks or exposure excursions.
                        self.recorder.append(
                            "NONEXECUTABLE_LATE_BAR",
                            {
                                "symbol": symbol,
                                "bar_start": row["date"].isoformat(),
                                "received_at": received.isoformat(),
                                "retained_mark_time": self.broker._mark_times[
                                    symbol
                                ].isoformat(),
                            },
                        )
                        possible_execution = any(
                            order["symbol"] == symbol
                            and order["submitted_at"] <= row["date"]
                            and order["role"] == "REDUCTION"
                            for order in self.broker.pending_orders
                        )
                        if position and position.get("sl") is not None:
                            possible_execution |= (
                                row["low"] <= position["sl"]
                                if position["direction"] == "BUY"
                                else row["high"] >= position["sl"]
                            )
                        if possible_execution:
                            self.recorder.failure(
                                "LATE_BAR_EXECUTION_CANNOT_BE_RECONSTRUCTED"
                            )
                    else:
                        candles[symbol] = row
                decision_at = self.recorder.now()
                for symbol, managed in self.runner.positions.items():
                    contexts[symbol] = self.context_service.build(
                        managed.thesis.instrument_id,
                        [
                            row
                            for row in self.history.get(symbol, [])
                            if row["date"]
                            >= decision_at
                            - timedelta(days=PRODUCTION_CANDLE_HISTORY_DAYS)
                        ],
                        decision_at,
                        received_at=received,
                    )
                before = len(self.runner.recorded_decisions)
                before_results = len(self.runner.execution_results)
                self.runner.on_event(decision_at, candles=candles, contexts=contexts)
                # Bar-based simulated fills become known when this event arrives.
                for fill in self.broker.fills:
                    self.fill_receipts.setdefault(fill["fill_id"], received.isoformat())
                records = self.runner.recorded_decisions[before:]
                results = self.runner.execution_results[before_results:]
                for record in records:
                    self.recorder.decision(record, received_at=received)
                self.decision_count += len(records)
                self._index_execution_results(records, results)
                self.recorder.append("PAPER_ACCOUNT_MARK", self.runner.equity_curve[-1])
                self._capture_facts()
                # These are CandidateRunner's offline reporting histories. Its
                # state machine, broker and coordinator do not read them. Paper
                # retains every original decision on recorder disk and keeps
                # only the latest diagnostic item here; offline studies keep
                # CandidateRunner's original complete-history behavior.
                for history in (
                    self.runner.recorded_decisions,
                    self.runner.execution_results,
                    self.runner.evaluations,
                    self.runner.equity_curve,
                ):
                    del history[:-1]
            else:
                raise ValueError("unknown paper stream event")
        except Exception:
            self.recorder.failed = True
            try:
                self.recorder.failure("PAPER_STREAM_EVENT_REJECTED")
            except Exception:
                pass
            raise

    def _entry(self, message, received):
        observed = self._fresh(message["quote_observed_at"], received)
        signal = message["signal"]
        symbol = signal["tradingsymbol"]
        if not self.runner.clock.snapshot(received).entries_allowed:
            raise ValueError("paper entry lies outside the permitted entry session")
        if symbol in self.runner.positions:
            raise ValueError(
                "paper session requires a new account for another symbol epoch"
            )
        key = self.broker._key_for(symbol).as_string() + ":" + uuid.uuid4().hex
        thesis = capture_entry_thesis(
            signal,
            position_key=key,
            trade_id=uuid.uuid4().hex,
            position_epoch=key.rsplit(":", 1)[1],
            instrument_id=f"SIM-{symbol}",
            effective_config=message.get("effective_config", {}),
            created_at=received,
        )
        price = self.broker._finite_price(message["quote_price"])
        quantity = self.broker._quantity(message["quantity"])
        entry_context = signal.get("market_context", {})
        if entry_context:
            decision = _at(
                entry_context.get("decision_event_time")
                or entry_context.get("decision_at")
                or entry_context.get("decisionAt")
            )
            if (
                not 0
                <= (received - decision).total_seconds()
                <= self.context_policy.max_primary_age_seconds
            ):
                raise ValueError("entry context is not a contemporaneous paper signal")
        _causal_snapshot(entry_context, received)
        _causal_snapshot(signal.get("entry_input", {}), received)
        calculate_provisional_risk(
            thesis,
            fill_price=self.broker._slipped(price, thesis.direction),
            filled_quantity=quantity,
        )
        self.recorder.append(
            "ENTRY_REFERENCE_QUOTE",
            {
                "position_key": key,
                "observed_at": observed.isoformat(),
                "received_at": received.isoformat(),
                "price": price,
                "entry_model": "IMMEDIATE_QUOTE_CHECKPOINT_WITH_DECLARED_SLIPPAGE",
            },
        )
        self.broker.place_market_order(
            symbol,
            thesis.direction,
            quantity,
            price,
            received,
            {"stopLoss": thesis.initial_stop, "strategy": thesis.strategy},
        )
        position = self.broker.positions[symbol]
        fills = [
            fill
            for fill in self.broker.fills
            if fill["order_id"] in position["entry_order_ids"]
        ]
        thesis = bind_terminal_fill(
            thesis,
            entry_vwap=position["entry_price"],
            filled_quantity=position["initial_quantity"],
            terminal_at=received,
            source_fill_ids=tuple(fill["fill_id"] for fill in fills),
        )
        state = PositionState(
            key,
            exposure=ExposureState.OPEN,
            thesis_health=ThesisHealth.VALID
            if thesis.management_profile.name != "unknown_legacy_bounded"
            else ThesisHealth.UNKNOWN,
            protection=ProtectionState.ACTIVE,
            known_quantity=position["quantity"],
        )
        self.runner.register_position(thesis=thesis, state=state, policy=self.policy)
        for fill in fills:
            self.fill_receipts[fill["fill_id"]] = received.isoformat()
        self._capture_facts()

    def _index_execution_results(self, records, results):
        if not results:
            return
        keys = {
            record["decision_id"]: record["trace"]["input_snapshot"]["state"][
                "position_key"
            ]
            for record in records
        }
        for result in results:
            key = keys[result["decision_id"]]
            links = self._candidate_links.setdefault(key, {})
            link = links.setdefault(result["intent_id"], {"orders": set()})
            if result.get("order_id"):
                link["orders"].add(result["order_id"])
        self.recorder.append("PAPER_EXECUTION_RESULTS", {"results": results})

    def _capture_facts(self):
        positions, intents, fills = [], [], []
        for symbol, managed in self.runner.positions.items():
            key = managed.state.position_key
            # CandidateRunner fixed-entry sessions allow exactly one epoch per symbol.
            orders = [
                order
                for order in self.broker.orders.values()
                if order["symbol"] == symbol
            ]
            candidate_links = self._candidate_links.get(key, {})
            candidate_orders = {
                order_id: intent_id
                for intent_id, link in candidate_links.items()
                for order_id in link["orders"]
            }
            for intent_id, link in candidate_links.items():
                projection = self.journal.get_order_intent_projection(intent_id)
                intents.append(
                    {
                        "intent_id": intent_id,
                        "position_key": key,
                        "origin": "CANDIDATE",
                        # Each exact decision join is already retained once in
                        # PAPER_EXECUTION_RESULTS. Offline export reconstructs
                        # the union without quadratic cumulative FACTS arrays.
                        "decision_ids": [],
                        "order_ids": sorted(
                            order_id
                            for order_id in link["orders"]
                            if candidate_orders[order_id] == intent_id
                        ),
                        "status": projection["state"],
                    }
                )
            for order in orders:
                superseded = order["order_id"] in candidate_orders
                if superseded and order["role"] != "PROTECTION":
                    continue
                # Retain the original obligation but transfer the current order
                # join after an acknowledged in-place candidate amendment.
                intents.append(
                    {
                        "intent_id": "paper-order:" + order["order_id"],
                        "position_key": key,
                        "origin": "ENTRY" if order["role"] == "ENTRY" else "PROTECTION",
                        "decision_ids": [],
                        "order_ids": [] if superseded else [order["order_id"]],
                        "status": "CLOSED" if superseded else order["status"],
                    }
                )
            position_fills = [
                fill for fill in self.broker.fills if fill["symbol"] == symbol
            ]
            for fill in position_fills:
                fills.append(
                    {
                        **fill,
                        "position_key": key,
                        "received_at": self.fill_receipts[fill["fill_id"]],
                    }
                )
            positions.append(
                {
                    "position_key": key,
                    "namespace": "PAPER",
                    "direction": managed.thesis.direction,
                    "initial_quantity": managed.thesis.fill_binding.filled_quantity,
                    "final_quantity": self.broker.positions.get(symbol, {}).get(
                        "quantity", 0
                    ),
                    "reconciliation_complete": managed.state.exposure
                    is ExposureState.CLOSED,
                    "opened_at": managed.thesis.fill_binding.entry_terminal_at,
                    "reconciled_at": self.recorder.now().isoformat(),
                    "working_order_ids": [
                        order["order_id"]
                        for order in orders
                        if order["status"]
                        not in {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"}
                    ],
                }
            )
        self.recorder.facts(positions=positions, intents=intents, fills=fills)

    def finish(self):
        self._capture_facts()
        result = self.recorder.finish()
        self.journal._get_conn().close()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    session = RealtimePaperSession(
        args.directory, plan=json.loads(_isolated(args.plan).read_text())
    )
    incoming = queue.Queue(maxsize=1000)

    def receive():
        try:
            for line in sys.stdin:
                incoming.put(("line", line, session.recorder.received()))
            incoming.put(("end", None, None))
        except Exception:
            incoming.put(("error", None, None))

    threading.Thread(target=receive, daemon=True, name="paper-market-feed").start()
    last_supervision = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_supervision >= 1:
                session.ingest({"type": "clock"})
                last_supervision = time.monotonic()
            try:
                kind, line, received_at = incoming.get(timeout=1.0)
            except queue.Empty:
                session.ingest({"type": "clock"})
                continue
            if kind == "end":
                break
            if kind == "error":
                raise OSError("paper input stream could not be read")
            if line.strip():
                session.ingest(json.loads(line), _received_at=received_at)
    except BaseException:
        session.recorder.failure("PAPER_STREAM_INTERRUPTED")
        raise
    finally:
        result = session.finish()
        with _isolated(args.output).open("x") as target:
            json.dump(result, target, sort_keys=True, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
