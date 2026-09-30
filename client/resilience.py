"""Retry, circuit breaking and endpoint failover.

Retry policy is per operation rather than global, because auction.proto has no
idempotency key and not every write is safe to resend:

Placing a bid is safe to repeat, because the server rejects any bid that does
not exceed the current highest, so a duplicate cannot take effect twice.
Closing an auction is safe, because applying it twice has no further effect.
Reads and signing out are safe. Signing in is safe, although each attempt
issues a new token and abandons the previous one. Creating an auction is not
safe, because the server generates a new identifier on every call and a repeat
would produce a second auction.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import grpc

from client.config import CONFIG
from client.errors import AuctionError, ErrorKind, classify_rpc_error

log = logging.getLogger("auction.client.resilience")


@dataclass(frozen=True)
class RetryPolicy:
    name: str
    retry_on_retryable: bool = True
    retry_on_never_sent: bool = True
    max_attempts: int | None = None

    @classmethod
    def idempotent(cls, name):
        return cls(name=name)

    @classmethod
    def at_most_once(cls, name):
        return cls(name=name, retry_on_retryable=False, retry_on_never_sent=True)


IDEMPOTENT = RetryPolicy.idempotent
AT_MOST_ONCE = RetryPolicy.at_most_once


def _never_reached_server(err):
    """True when we can prove the request never got to the server.

    A timeout does not qualify, because the server may have applied the change
    and merely answered slowly.
    """
    if err.kind is not ErrorKind.UNAVAILABLE:
        return False

    detail = (err.message or "").lower()
    return any(m in detail for m in (
        "connection refused",
        "failed to connect",
        "connect failed",
        "name resolution",
        "socket closed",
        "transport closed",
    ))


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-endpoint failure gate."""

    def __init__(self, endpoint, config):
        self.endpoint = endpoint
        self._config = config
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at = 0.0

    @property
    def state(self):
        with self._lock:
            return self._observe_locked()

    def _observe_locked(self):
        if (self._state is BreakerState.OPEN
                and time.monotonic() - self._opened_at >= self._config.reset_timeout_s):
            self._state = BreakerState.HALF_OPEN
            self._successes = 0
            log.info("breaker for %s is probing after cooldown", self.endpoint)
        return self._state

    def allows_request(self):
        with self._lock:
            return self._observe_locked() is not BreakerState.OPEN

    def record_success(self):
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self._config.success_threshold:
                    log.info("breaker for %s closed, endpoint healthy again", self.endpoint)
                    self._state = BreakerState.CLOSED
                    self._failures = 0
            else:
                self._failures = 0

    def record_failure(self):
        with self._lock:
            self._failures += 1

            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                log.warning("breaker for %s reopened, probe failed", self.endpoint)
            elif self._failures >= self._config.failure_threshold:
                if self._state is not BreakerState.OPEN:
                    log.warning("breaker for %s opened after %d failures", self.endpoint, self._failures)
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()


@dataclass
class _Node:
    endpoint: str
    breaker: CircuitBreaker
    channel: grpc.Channel | None = None
    stub: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class NodePool:
    """Holds a channel per application server and decides which one to use.

    With a single endpoint this is one channel plus a breaker; the ordering
    and failover logic only comes into play once there are several.
    """

    def __init__(self, config=None, stub_factory=None):
        self._config = config or CONFIG
        if not self._config.endpoints:
            raise ValueError("no endpoints configured")

        if stub_factory is None:
            from generated import auction_pb2_grpc
            stub_factory = auction_pb2_grpc.AuctionServiceStub
        self._stub_factory = stub_factory

        self._nodes = [
            _Node(endpoint=e, breaker=CircuitBreaker(e, self._config.breaker))
            for e in self._config.endpoints
        ]
        self._preferred = 0
        self._pref_lock = threading.Lock()

    @property
    def endpoints(self):
        return [n.endpoint for n in self._nodes]

    def health(self):
        return {n.endpoint: n.breaker.state.value for n in self._nodes}

    @property
    def preferred_endpoint(self):
        with self._pref_lock:
            return self._nodes[self._preferred].endpoint

    def all_unavailable(self):
        return all(not n.breaker.allows_request() for n in self._nodes)

    def _stub_for(self, node):
        with node.lock:
            if node.stub is None:
                node.channel = grpc.insecure_channel(
                    node.endpoint, options=self._config.grpc_channel_options()
                )
                node.stub = self._stub_factory(node.channel)
            return node.stub

    def _drop_channel(self, node):
        with node.lock:
            if node.channel is not None:
                try:
                    node.channel.close()
                except Exception:
                    pass
            node.channel = None
            node.stub = None

    def prefer(self, endpoint):
        for i, node in enumerate(self._nodes):
            if node.endpoint == endpoint:
                with self._pref_lock:
                    if self._preferred != i:
                        log.info("now preferring endpoint %s", endpoint)
                    self._preferred = i
                return

    def _advance(self):
        with self._pref_lock:
            self._preferred = (self._preferred + 1) % len(self._nodes)

    def candidates(self):
        """Preferred first; open-breaker nodes pushed to the back.

        They stay in the list rather than being dropped so a total outage
        still produces a real connection error instead of "no candidates".
        """
        with self._pref_lock:
            start = self._preferred

        ordered = [self._nodes[(start + i) % len(self._nodes)] for i in range(len(self._nodes))]
        healthy = [n for n in ordered if n.breaker.allows_request()]
        blocked = [n for n in ordered if not n.breaker.allows_request()]
        return healthy + blocked

    def close(self):
        for node in self._nodes:
            self._drop_channel(node)


def _backoff_delay(attempt, cfg):
    # The delay is fully randomised. Without that, clients which lose a server
    # at the same moment would all retry on the same schedule and overwhelm its
    # replacement together.
    ceiling = min(cfg.max_backoff_s, cfg.initial_backoff_s * (cfg.multiplier ** attempt))
    return random.uniform(0, ceiling) if cfg.jitter else ceiling


def call(pool, policy, invoke, config=None, on_retry=None):
    """Run invoke(stub, endpoint) with retry, backoff and failover.

    invoke must perform exactly one RPC. Raises the final AuctionError once
    attempts are exhausted.
    """
    cfg = config or CONFIG
    max_attempts = policy.max_attempts or cfg.retry.max_attempts

    last = None
    attempt = 0

    while attempt < max_attempts:
        node = pool.candidates()[0]
        stub = pool._stub_for(node)

        try:
            result = invoke(stub, node.endpoint)
        except grpc.RpcError as exc:
            err = classify_rpc_error(exc, endpoint=node.endpoint)
        except AuctionError as exc:
            err = exc
        else:
            node.breaker.record_success()
            pool.prefer(node.endpoint)
            return result

        last = err
        node.breaker.record_failure()

        if err.kind in (ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT):
            pool._drop_channel(node)

        if err.leader_hint:
            pool.prefer(err.leader_hint)
        elif err.kind.should_failover and len(pool.endpoints) > 1:
            pool._advance()

        may_retry = ((policy.retry_on_retryable and err.retryable)
                     or (policy.retry_on_never_sent and _never_reached_server(err)))

        attempt += 1
        if not may_retry or attempt >= max_attempts:
            break

        delay = _backoff_delay(attempt, cfg.retry)
        log.warning(
            "%s failed on %s (%s); retry %d/%d in %.2fs",
            policy.name, node.endpoint, err.kind.value, attempt, max_attempts, delay,
        )
        if on_retry:
            on_retry(err, attempt, delay)
        time.sleep(delay)

    # Losing every server is reported separately from losing one of them,
    # because the two situations call for different responses.
    if last.kind in (ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT) and pool.all_unavailable():
        last = AuctionError(
            ErrorKind.PARTITIONED,
            f"all {len(pool.endpoints)} endpoint(s) unreachable: {last.message}",
            endpoint=last.endpoint,
            attempts=attempt,
            cause=last.cause,
        )

    last.attempts = attempt
    raise last
