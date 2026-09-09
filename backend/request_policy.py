import enum
import random
import threading
import time
from typing import Any, Callable, Optional

from .utils import push_log


class Priority(enum.IntEnum):
    CRITICAL = 1  # Emergency flatten, protective stops, manual cancels
    ORDER = 2  # Entry orders, exits
    RECONCILE = 3  # get_positions, get_orders, get_margins
    ANALYTICS = 4  # historical data, quotes, instruments


class ErrorClassification(enum.Enum):
    RETRYABLE = 1
    NON_RETRYABLE = 2
    AMBIGUOUS = 3  # E.g. timeout on an order placement


class BrokerGateway:
    def __init__(
        self,
        rate_limit: float = 3.0,  # Requests per second
        max_retries: int = 3,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset_seconds: int = 30,
    ):
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_reset_seconds = circuit_breaker_reset_seconds

        self._lock = threading.RLock()
        self._tokens = float(rate_limit)
        self._last_update = time.time()
        self._condition = threading.Condition(self._lock)

        # Priority waiting counts to prevent low-priority from acquiring when high-priority is waiting
        self._waiting = {p: 0 for p in Priority}

        self.consecutive_failures = 0
        self.circuit_open = False
        self.circuit_open_time = 0.0
        # HALF_OPEN: one probe request is in-flight; circuit_open remains True
        # until the probe succeeds (→ CLOSED) or fails (→ OPEN with fresh timestamp).
        self.circuit_half_open = False

        self._last_auth_error_log_time: float = 0.0
        self._auth_error_dedup_seconds: float = 60.0

    def _update_tokens(self):
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(
            float(self.rate_limit), self._tokens + elapsed * self.rate_limit
        )
        self._last_update = now

    def _acquire_token(self, priority: Priority):
        with self._condition:
            self._waiting[priority] += 1
            try:
                while True:
                    self._update_tokens()

                    # Check if there's any higher priority waiting
                    higher_waiting = any(
                        self._waiting[p] > 0 for p in Priority if p < priority
                    )

                    if not higher_waiting and self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return

                    if not higher_waiting and self._tokens < 1.0:
                        sleep_time = (1.0 - self._tokens) / self.rate_limit
                    else:
                        # Wait for a higher priority request to finish and wake us
                        sleep_time = 0.1

                    self._condition.wait(timeout=sleep_time)
            finally:
                self._waiting[priority] -= 1
                # Wake up others since we are done acquiring and modifying counts
                self._condition.notify_all()

    def _check_circuit(self, priority: Priority):
        with self._lock:
            if self.circuit_open:
                now = time.time()
                if now - self.circuit_open_time > self.circuit_breaker_reset_seconds:
                    if not self.circuit_half_open:
                        # Transition to HALF_OPEN: allow a single probe through.
                        self.circuit_half_open = True
                        push_log(
                            "Circuit breaker entering HALF_OPEN — sending probe request.",
                            level="info",
                        )
                        return  # let this request through
                    else:
                        # A probe is already in-flight; block non-critical requests.
                        if priority > Priority.CRITICAL:
                            raise Exception(
                                "Circuit breaker is HALF_OPEN. Probe already in-flight; rejecting non-critical request."
                            )
                else:
                    if priority > Priority.CRITICAL:
                        raise Exception(
                            "Circuit breaker is open. Rejecting non-critical request."
                        )

    def _record_success(self):
        with self._lock:
            if self.circuit_open:
                push_log(
                    "Circuit breaker probe succeeded — reset to CLOSED.", level="info"
                )
            self.circuit_open = False
            self.circuit_half_open = False
            self.consecutive_failures = 0

    def _record_failure(self):
        with self._lock:
            self.consecutive_failures += 1
            if self.circuit_half_open:
                # Probe failed: reopen with a fresh timestamp so the timer resets.
                # Also reset consecutive_failures so the elif threshold branch cannot
                # spuriously trigger on the next call (the probe outcome is independent
                # of the pre-open failure streak).
                self.circuit_half_open = False
                self.consecutive_failures = 0
                self.circuit_open_time = time.time()
                push_log(
                    "Circuit breaker probe FAILED — reopened with fresh timer.",
                    level="error",
                )
            elif (
                self.consecutive_failures >= self.circuit_breaker_threshold
                and not self.circuit_open
            ):
                self.circuit_open = True
                self.circuit_open_time = time.time()
                push_log(
                    f"Circuit breaker OPENED after {self.consecutive_failures} failures.",
                    level="error",
                )

    def _classify_error(self, e: Exception) -> ErrorClassification:
        err_type = type(e).__name__
        err_msg = str(e).lower()

        non_retryable = [
            "TokenException",
            "PermissionException",
            "InputException",
            "DataException",
        ]
        if err_type in non_retryable or any(
            x in err_msg for x in ["token", "permission", "input", "invalid", "api key"]
        ):
            return ErrorClassification.NON_RETRYABLE

        if (
            "timeout" in err_msg
            or "timed out" in err_msg
            or "readtimeout" in err_msg
            or "connectionerror" in err_msg
        ):
            return ErrorClassification.AMBIGUOUS

        return ErrorClassification.RETRYABLE

    def execute(
        self,
        action: Callable,
        priority: Priority,
        is_order: bool = False,
        order_reconciler: Optional[Callable] = None,
        *args,
        **kwargs,
    ) -> Any:
        attempt = 0
        backoff = 0.5

        while True:
            self._check_circuit(priority)
            self._acquire_token(priority)

            start_time = time.time()
            try:
                result = action(*args, **kwargs)
                latency = time.time() - start_time
                if attempt > 0:
                    push_log(
                        f"Broker request succeeded after {attempt} retries (latency: {latency:.2f}s).",
                        level="info",
                    )
                self._record_success()
                return result

            except Exception as e:
                latency = time.time() - start_time

                classification = self._classify_error(e)
                if classification != ErrorClassification.NON_RETRYABLE:
                    self._record_failure()

                if (
                    classification == ErrorClassification.AMBIGUOUS
                    and is_order
                    and order_reconciler
                ):
                    push_log(
                        f"Ambiguous order error (timeout). Attempting reconciliation. Error: {e}",
                        level="warning",
                    )
                    try:
                        res = order_reconciler()
                        if res:
                            push_log(
                                f"Order reconciliation successful. Broker accepted order {res}.",
                                level="info",
                            )
                            return res
                    except Exception as rec_err:
                        push_log(
                            f"Order reconciliation failed: {rec_err}", level="error"
                        )

                    classification = ErrorClassification.RETRYABLE

                if (
                    classification == ErrorClassification.NON_RETRYABLE
                    or attempt >= self.max_retries
                ):
                    err_lower = str(e).lower()
                    is_auth_error = any(
                        x in err_lower
                        for x in [
                            "api_key",
                            "access_token",
                            "tokenexception",
                            "invalid token",
                        ]
                    )
                    if is_auth_error:
                        now = time.time()
                        with self._lock:
                            since_last = now - self._last_auth_error_log_time
                            should_log = since_last >= self._auth_error_dedup_seconds
                            if should_log:
                                self._last_auth_error_log_time = now
                        if should_log:
                            push_log(
                                "Kite session has expired. Please log in again to resume trading.",
                                level="warning",
                            )
                    else:
                        push_log(
                            f"Broker request failed (Priority {priority.name}, Attempt {attempt + 1}/{self.max_retries + 1}): {e}",
                            level="error",
                        )
                    raise e

                attempt += 1
                sleep_time = backoff * (1 + random.uniform(0, 0.2))  # Jitter
                push_log(
                    f"Broker request retryable error (Attempt {attempt}). Sleeping {sleep_time:.2f}s. Error: {e}",
                    level="warning",
                )
                time.sleep(sleep_time)
                backoff *= 2


broker_gateway = BrokerGateway(rate_limit=3.0, max_retries=3)
