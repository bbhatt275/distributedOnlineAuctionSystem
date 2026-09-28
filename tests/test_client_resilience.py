"""Retry / breaker / failover tests, using a fake stub that raises scripted errors."""

from __future__ import annotations

import time

import grpc
import pytest

from client.config import BreakerConfig, ClientConfig, RetryConfig
from client.errors import AuctionError, ErrorKind, classify_rpc_error, classify_status
from client.resilience import (
    AT_MOST_ONCE,
    IDEMPOTENT,
    BreakerState,
    CircuitBreaker,
    NodePool,
    call,
)


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "") -> None:
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


def fast_config(**overrides) -> ClientConfig:
    base = dict(
        endpoints=["node-a:1"],
        rpc_timeout_s=0.1,
        retry=RetryConfig(max_attempts=3, initial_backoff_s=0.001, max_backoff_s=0.002),
        breaker=BreakerConfig(failure_threshold=2, reset_timeout_s=0.05),
    )
    base.update(overrides)
    return ClientConfig(**base)


def pool_for(config: ClientConfig) -> NodePool:
    return NodePool(config, stub_factory=lambda channel: object())


@pytest.mark.parametrize(
    "code,expected",
    [
        (grpc.StatusCode.UNAVAILABLE, ErrorKind.UNAVAILABLE),
        (grpc.StatusCode.DEADLINE_EXCEEDED, ErrorKind.TIMEOUT),
        (grpc.StatusCode.UNAUTHENTICATED, ErrorKind.UNAUTHENTICATED),
        (grpc.StatusCode.NOT_FOUND, ErrorKind.NOT_FOUND),
    ],
)
def test_grpc_codes_are_classified(code, expected):
    assert classify_rpc_error(FakeRpcError(code)).kind is expected


def test_servicer_exception_is_not_retried():
    err = classify_rpc_error(
        FakeRpcError(grpc.StatusCode.UNKNOWN, "Exception calling application: boom")
    )
    assert err.kind is ErrorKind.SERVER_BUG
    assert err.retryable is False


def test_known_server_typo_is_reported_as_auth_failure():
    # grpc_service.py Post() has a 'mesage' typo, so a stale token arrives
    # as a servicer crash. See docs/server-issues.md.
    err = classify_rpc_error(
        FakeRpcError(
            grpc.StatusCode.UNKNOWN,
            'Exception calling application: Protocol message StatusResponse has no "mesage" field.',
        )
    )
    assert err.kind is ErrorKind.UNAUTHENTICATED


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Bid must be higher than the current highest bid", ErrorKind.BID_TOO_LOW),
        ("Auction not found", ErrorKind.NOT_FOUND),
        ("Auction is not active", ErrorKind.AUCTION_NOT_ACTIVE),
        ("Auction has ended", ErrorKind.AUCTION_NOT_ACTIVE),
        ("Invalid credentials", ErrorKind.UNAUTHENTICATED),
        ("Not Authenticated", ErrorKind.UNAUTHENTICATED),
        ("Starting price cannot be negative", ErrorKind.INVALID_ARGUMENT),
    ],
)
def test_server_messages_are_classified(message, expected):
    assert classify_status(message).kind is expected


def test_leader_hint_is_extracted():
    err = classify_status("not leader, leader is node-b:50052")
    assert err.kind is ErrorKind.NOT_LEADER
    assert err.leader_hint == "node-b:50052"


def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker("n", BreakerConfig(failure_threshold=2, reset_timeout_s=10))
    breaker.record_failure()
    assert breaker.allows_request()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows_request()


def test_breaker_half_opens_after_cooldown_then_closes():
    config = BreakerConfig(failure_threshold=1, reset_timeout_s=0.02, success_threshold=2)
    breaker = CircuitBreaker("n", config)
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    time.sleep(0.03)
    assert breaker.state is BreakerState.HALF_OPEN

    breaker.record_success()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens_breaker():
    config = BreakerConfig(failure_threshold=1, reset_timeout_s=0.02)
    breaker = CircuitBreaker("n", config)
    breaker.record_failure()
    time.sleep(0.03)
    assert breaker.state is BreakerState.HALF_OPEN

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN


def test_success_resets_failure_count():
    breaker = CircuitBreaker("n", BreakerConfig(failure_threshold=2, reset_timeout_s=10))
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allows_request()


def test_transient_failure_is_retried_then_succeeds():
    config = fast_config()
    pool = pool_for(config)
    attempts = []

    def invoke(stub, endpoint):
        attempts.append(endpoint)
        if len(attempts) < 3:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "connection refused")
        return "ok"

    assert call(pool, IDEMPOTENT("Test"), invoke, config=config) == "ok"
    assert len(attempts) == 3


def test_retries_are_capped_and_error_reports_attempts():
    config = fast_config()
    pool = pool_for(config)
    attempts = []

    def invoke(stub, endpoint):
        attempts.append(endpoint)
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "connection refused")

    with pytest.raises(AuctionError) as excinfo:
        call(pool, IDEMPOTENT("Test"), invoke, config=config)

    assert len(attempts) == 3
    assert excinfo.value.attempts == 3


def test_non_retryable_domain_error_fails_immediately():
    config = fast_config()
    pool = pool_for(config)
    attempts = []

    def invoke(stub, endpoint):
        attempts.append(endpoint)
        raise classify_status("Bid must be higher than the current highest bid")

    with pytest.raises(AuctionError) as excinfo:
        call(pool, IDEMPOTENT("PlaceBid"), invoke, config=config)

    assert len(attempts) == 1
    assert excinfo.value.kind is ErrorKind.BID_TOO_LOW


def test_at_most_once_policy_does_not_retry_an_ambiguous_timeout():
    """A timeout may mean the write landed -- CreateAuction must not resend."""
    config = fast_config()
    pool = pool_for(config)
    attempts = []

    def invoke(stub, endpoint):
        attempts.append(endpoint)
        raise FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")

    with pytest.raises(AuctionError):
        call(pool, AT_MOST_ONCE("CreateAuction"), invoke, config=config)

    assert len(attempts) == 1


def test_at_most_once_policy_does_retry_when_nothing_was_sent():
    """Connection refused proves no server code ran, so resending is safe."""
    config = fast_config()
    pool = pool_for(config)
    attempts = []

    def invoke(stub, endpoint):
        attempts.append(endpoint)
        if len(attempts) < 2:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "failed to connect to all addresses")
        return "ok"

    assert call(pool, AT_MOST_ONCE("CreateAuction"), invoke, config=config) == "ok"
    assert len(attempts) == 2


def test_failover_moves_to_a_healthy_endpoint():
    config = fast_config(endpoints=["dead:1", "live:2"])
    pool = pool_for(config)
    seen = []

    def invoke(stub, endpoint):
        seen.append(endpoint)
        if endpoint == "dead:1":
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "connection refused")
        return "ok"

    assert call(pool, IDEMPOTENT("Test"), invoke, config=config) == "ok"
    assert seen == ["dead:1", "live:2"]
    assert pool.preferred_endpoint == "live:2"


def test_leader_hint_redirects_without_round_robin():
    config = fast_config(endpoints=["follower:1", "other:2", "leader:3"])
    pool = pool_for(config)
    seen = []

    def invoke(stub, endpoint):
        seen.append(endpoint)
        if endpoint != "leader:3":
            raise classify_status("not leader, leader is leader:3")
        return "ok"

    assert call(pool, IDEMPOTENT("PlaceBid"), invoke, config=config) == "ok"
    # straight to the hinted leader, skipping other:2
    assert seen == ["follower:1", "leader:3"]


def test_total_outage_is_reported_as_partitioned():
    config = fast_config(
        endpoints=["a:1", "b:2"],
        retry=RetryConfig(max_attempts=4, initial_backoff_s=0.001, max_backoff_s=0.002),
        breaker=BreakerConfig(failure_threshold=1, reset_timeout_s=30),
    )
    pool = pool_for(config)

    def invoke(stub, endpoint):
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE, "connection refused")

    with pytest.raises(AuctionError) as excinfo:
        call(pool, IDEMPOTENT("Test"), invoke, config=config)

    assert excinfo.value.kind is ErrorKind.PARTITIONED
    assert pool.all_unavailable()
