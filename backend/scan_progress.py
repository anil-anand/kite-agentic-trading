"""Read-only, per-batch scanner telemetry; never participates in admission."""

import logging
import threading
import time
from copy import deepcopy

from .time_utils import now_utc

SCAN_WORKERS = 3


class ScanProgress:
    def __init__(self, on_update=None):
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._on_update = on_update
        self._last_published = float("-inf")
        self._thread_workers = {}
        self._data = {
            "phase": "preparing",
            "analysisOnly": False,
            "startedAt": now_utc().isoformat(),
            "completedAt": None,
            "nextScanAt": None,
            "universeSize": None,
            "totalSymbols": 0,
            "completedSymbols": 0,
            "evaluatedSymbols": 0,
            "skippedSymbols": 0,
            "failedSymbols": 0,
            "signalsFound": 0,
            "signalsPublished": 0,
            "enabledStrategies": [],
            "queuedSymbols": [],
            "workers": [
                {
                    "id": worker_id,
                    "symbol": None,
                    "stage": "idle",
                    "startedAt": None,
                    "updatedAt": None,
                }
                for worker_id in range(1, SCAN_WORKERS + 1)
            ],
            "results": [],
            "message": None,
        }

    def snapshot(self):
        with self._lock:
            return deepcopy(self._data)

    def _publish(self, *, force=False):
        if self._on_update is None:
            return
        # Serialize notifications, outside the data lock. The callback may
        # read status, which takes a new snapshot. Coalesce incidental counters;
        # stage transitions always publish, including before slow data requests.
        with self._publish_lock:
            current = time.monotonic()
            if not force and current - self._last_published < 0.5:
                return
            self._last_published = current
            try:
                self._on_update()
            except Exception:
                logging.getLogger(__name__).warning("Scan progress delivery failed")

    def update(self, *, force=False, **fields):
        with self._lock:
            self._data.update(fields)
        self._publish(force=force)

    def set_symbols(self, symbols, enabled_strategies):
        self.update(
            force=True,
            phase="loading_instruments",
            totalSymbols=len(symbols),
            queuedSymbols=list(symbols),
            enabledStrategies=list(enabled_strategies),
        )

    def start_symbol(self, symbol):
        with self._lock:
            thread_id = threading.get_ident()
            worker_index = self._thread_workers.setdefault(
                thread_id, len(self._thread_workers)
            )
            now = now_utc().isoformat()
            self._data["phase"] = "scanning"
            self._data["queuedSymbols"].remove(symbol)
            self._data["workers"][worker_index].update(
                symbol=symbol,
                stage="waiting_for_symbol",
                startedAt=now,
                updatedAt=now,
            )
        self._publish(force=True)
        return worker_index

    def worker_stage(self, worker_index, stage):
        with self._lock:
            self._data["workers"][worker_index].update(
                stage=stage, updatedAt=now_utc().isoformat()
            )
        self._publish(force=True)

    def finish_symbol(
        self, worker_index, symbol, outcome, detail, *, signals=0, candle_time=None
    ):
        with self._lock:
            self._data["results"].append(
                {
                    "symbol": symbol,
                    "outcome": outcome,
                    "detail": detail,
                    "signals": signals,
                    "candleTime": candle_time,
                }
            )
            self._data["completedSymbols"] += 1
            self._data["signalsFound"] += signals
            counter = (
                "evaluatedSymbols"
                if outcome in {"signals", "no_match"}
                else "failedSymbols"
                if outcome == "error"
                else "skippedSymbols"
            )
            self._data[counter] += 1
            self._data["workers"][worker_index].update(
                symbol=None,
                stage="idle",
                startedAt=None,
                updatedAt=now_utc().isoformat(),
            )
        self._publish(force=True)

    def signal_published(self):
        with self._lock:
            self._data["signalsPublished"] += 1
        self._publish()

    def finish(self, *, error=None):
        self.update(
            force=True,
            phase="error" if error else "completed",
            completedAt=now_utc().isoformat(),
            message=error,
        )
