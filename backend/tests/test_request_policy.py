import threading
import time

import pytest

from backend.request_policy import BrokerGateway, ErrorClassification, Priority


def test_rate_limiter_basic():
    gateway = BrokerGateway(rate_limit=10.0, max_retries=1)

    start_time = time.time()

    # Execute 5 requests. With rate_limit=10, 5 requests should take 0.5s if it starts with full tokens
    # Actually if tokens=10.0, the first 10 requests are instantaneous
    for _ in range(5):
        gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)

    elapsed = time.time() - start_time
    assert elapsed < 0.2  # Should be very fast due to initial tokens


def test_rate_limiter_throttling():
    # Only 2 requests per second
    gateway = BrokerGateway(rate_limit=2.0, max_retries=1)

    # Drain initial tokens
    gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)
    gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)

    start_time = time.time()
    gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)
    elapsed = time.time() - start_time

    # Third request must wait for 1 token = 1/2 second = 0.5s
    assert elapsed > 0.4


def test_priority_handling():
    gateway = BrokerGateway(rate_limit=2.0, max_retries=1)

    # Drain initial tokens
    gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)
    gateway.execute(lambda: "ok", priority=Priority.ANALYTICS)

    results = []

    def worker_analytics():
        gateway.execute(
            lambda: results.append("ANALYTICS"), priority=Priority.ANALYTICS
        )

    def worker_critical():
        gateway.execute(lambda: results.append("CRITICAL"), priority=Priority.CRITICAL)

    t1 = threading.Thread(target=worker_analytics)
    t2 = threading.Thread(target=worker_critical)

    # Start analytics first. It will wait since tokens are 0.
    t1.start()
    time.sleep(0.1)  # ensure t1 is waiting
    t2.start()  # Start critical. It should get the next token before analytics.

    t1.join()
    t2.join()

    # The critical one should finish first
    assert results == ["CRITICAL", "ANALYTICS"]


def test_error_classification():
    gateway = BrokerGateway()

    class CustomError(Exception):
        pass

    assert (
        gateway._classify_error(CustomError("Network error: read timeout"))
        == ErrorClassification.AMBIGUOUS
    )
    assert (
        gateway._classify_error(CustomError("Token is invalid"))
        == ErrorClassification.NON_RETRYABLE
    )
    assert (
        gateway._classify_error(CustomError("Random 502 error"))
        == ErrorClassification.RETRYABLE
    )


def test_circuit_breaker():
    gateway = BrokerGateway(
        rate_limit=10.0,
        max_retries=0,
        circuit_breaker_threshold=2,
        circuit_breaker_reset_seconds=1,
    )

    def failing_action():
        raise Exception("Random failure")

    # First failure
    with pytest.raises(Exception):
        gateway.execute(failing_action, priority=Priority.ANALYTICS)
    assert not gateway.circuit_open

    # Second failure - should trip circuit
    with pytest.raises(Exception):
        gateway.execute(failing_action, priority=Priority.ANALYTICS)

    assert gateway.circuit_open

    # Third failure - should fast fail for ANALYTICS
    with pytest.raises(Exception, match="Circuit breaker is open"):
        gateway.execute(failing_action, priority=Priority.ANALYTICS)

    # But CRITICAL should still try and just raise the original exception
    with pytest.raises(Exception, match="Random failure"):
        gateway.execute(failing_action, priority=Priority.CRITICAL)

    # Wait for reset
    time.sleep(1.1)

    # Now it should be half-open and allow ANALYTICS to try
    with pytest.raises(Exception, match="Random failure"):
        gateway.execute(failing_action, priority=Priority.ANALYTICS)


def test_order_reconciliation():
    gateway = BrokerGateway(rate_limit=10.0, max_retries=1)

    attempts = 0

    def placing_order():
        nonlocal attempts
        attempts += 1
        raise Exception("timeout placing order")

    def reconciler():
        # pretend the order was actually placed
        return "mock_order_id_123"

    result = gateway.execute(
        placing_order,
        priority=Priority.ORDER,
        is_order=True,
        order_reconciler=reconciler,
    )

    # Should not have retried since reconciler returned an ID
    assert attempts == 1
    assert result == "mock_order_id_123"
