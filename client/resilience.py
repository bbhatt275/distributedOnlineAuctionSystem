"""Retry, circuit breaking and endpoint failover.

Three cooperating pieces:

* ``CircuitBreaker`` -- per-endpoint health. Stops us dialling a node that is
  known to be down on every single call.
* ``NodePool``      -- ordered endpoints plus the channel/stub cache. Knows
  which node is currently preferred and rotates on failure.
* ``call``          -- the retry loop. Applies an operation's ``RetryPolicy``,
  exponential backoff with full jitter, and failover between attempts.

Why retry policy is per operation
---------------------------------
proto/auction.proto has no ``idempotency_key``, so blind retries are not
universally safe. Each operation is classified explicitly:

* ``PlaceBid``      -- SAFE. application/auction_manager.py rejects any bid
  where ``amount <= current_highest_bid``. Re-sending the same amount after an
  ambiguous failure is therefore rejected by the server's own monotonicity
  rule, so a duplicate cannot take effect. The retry may report BID_TOO_LOW
  where the first attempt actually won; ``AuctionClient.place_bid`` resolves
  that ambiguity by reading back the auction.
* ``CloseAuction``  -- SAFE. Sets ``active=False``; applying it twice is a
  no-op.
* ``Logout``/``Get*`` -- SAFE. Pure reads, or idempotent by construction.
* ``Login``         -- SAFE to retry, but each success mints a fresh token and
  orphans the previous one. Acceptable: tokens are in-memory only.
* ``CreateAuction`` -- NOT SAFE. The server mints a fresh ``uuid4`` per call,
  so a retry after an ambiguous failure creates a second auction. Retried only
  on failures that prove the call never ran (connect-refused), and otherwise
  reconciled by reading back; see ``AuctionClient.create_auction``.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

import grpc

from client.config import CONFIG, BreakerConfig, ClientConfig, RetryConfig
from client.errors import AuctionError, ErrorKind, classify_rpc_error

log = logging.getLogger("auction.client.resilience")

T = TypeVar("T")


# --- retry policy -------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    name: str
    # Retry on any error whose kind is retryable.
    retry_on_retryable: bool = True
    # Additionally retry when the failure proves the RPC never reached the
    # server (connection refused). Safe even for non-idempotent writes.
    retry_on_never_sent: bool = True
    max_attempts: int | None = None

    @classmethod
    def idempotent(cls, name: str) -> "RetryPolicy":
        return cls(name=name)

    @classmethod
    def at_most_once(cls, name: str) -> "RetryPolicy":
        """For non-idempotent writes: only retry if the call never landed."""
        return cls(name=name, retry_on_retryable=False, retry_on_never_sent=True)


IDEMPOTENT = RetryPolicy.idempotent
AT_MOST_ONCE = RetryPolicy.at_most_once


def _never_reached_server(err: AuctionError) -> bool:
    """True when we can prove the server never saw the request.

    ``UNAVAILABLE`` with a connect-level detail means the TCP connection was
    refused or the channel never came up, so no application code ran. A
    timeout is explicitly *not* included: the server may well have applied the
    write and simply answered too slowly.
    """
    if err.kind is not ErrorKind.UNAVAILABLE:
        return False
    detail = (err.message or "").lower()
    return any(
        marker in detail
        for marker in (
            "connection refused",
            "failed to connect",
            "connect failed",
            "name resolution",
            "socket closed",
            "transport closed",
        )
    )


# --- circuit breaker ----------------------------------------------------------


class BreakerState(str, Enum):
    CLOSED = "closed"        # healthy, traffic flows
    OPEN = "open"            # known bad, traffic blocked
    HALF_OPEN = "half_open"  # probing whether it recovered


class CircuitBreaker:
    """Per-endpoint failure gate. Thread-safe."""

    def __init__(self, endpoint: str, config: BreakerConfig) -> None:
        self.endpoint = endpoint
        self._config = config
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at = 0.0

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._observe_locked()

    def _observe_locked(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and time.monotonic() - self._opened_at >= self._config.reset_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._successes = 0
            log.info("breaker %s -> half_open (probing)", self.endpoint)
        return self._state

    def allows_request(self) -> bool:
        with self._lock:
            return self._observe_locked() is not BreakerState.OPEN

    def record_success(self) -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self._config.success_threshold:
                    log.info("breaker %s -> closed (recovered)", self.endpoint)
                    self._state = BreakerState.CLOSED
                    self._failures = 0
            else:
                self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN:
                # Probe failed: straight back to open, restart the cooldown.
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                log.warning("breaker %s -> open (probe failed)", self.endpoint)
            elif self._failures >= self._config.failure_threshold:
                if self._state is not BreakerState.OPEN:
                    log.warning(
                        "breaker %s -> open after %d consecutive failures",
                        self.endpoint,
                        self._failures,
                    )
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()


# --- node pool ----------------------------------------------------------------


@dataclass
class _Node:
    endpoint: str
    breaker: CircuitBreaker
    channel: grpc.Channel | None = None
    stub: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class NodePool:
    """Owns channels to every known application server and picks one to use.

    Milestone 1 is configured with a single endpoint, in which case this
    degenerates to "one channel plus a breaker". The ordering and failover
    logic is already here so Milestone 2 only has to lengthen the list.
    """

    def __init__(self, config: ClientConfig | None = None, stub_factory: Callable | None = None) -> None:
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

    # -- introspection, surfaced in the UI health indicator -------------------

    @property
    def endpoints(self) -> list[str]:
        return [n.endpoint for n in self._nodes]

    def health(self) -> dict[str, str]:
        return {n.endpoint: n.breaker.state.value for n in self._nodes}

    @property
    def preferred_endpoint(self) -> str:
        with self._pref_lock:
            return self._nodes[self._preferred].endpoint

    def all_unavailable(self) -> bool:
        return all(not n.breaker.allows_request() for n in self._nodes)

    # -- channel management ---------------------------------------------------

    def _stub_for(self, node: _Node):
        with node.lock:
            if node.stub is None:
                node.channel = grpc.insecure_channel(
                    node.endpoint, options=self._config.grpc_channel_options()
                )
                node.stub = self._stub_factory(node.channel)
            return node.stub

    def _drop_channel(self, node: _Node) -> None:
        """Tear down a channel so the next attempt re-resolves and reconnects."""
        with node.lock:
            if node.channel is not None:
                try:
                    node.channel.close()
                except Exception:  # pragma: no cover - close is best-effort
                    pass
            node.channel = None
            node.stub = None

    def prefer(self, endpoint: str) -> None:
        """Pin the preferred endpoint, e.g. after a NOT_LEADER leader hint."""
        for i, node in enumerate(self._nodes):
            if node.endpoint == endpoint:
                with self._pref_lock:
                    if self._preferred != i:
                        log.info("preferred endpoint -> %s", endpoint)
                    self._preferred = i
                return

    def _advance(self) -> None:
        with self._pref_lock:
            self._preferred = (self._preferred + 1) % len(self._nodes)

    def candidates(self) -> list[_Node]:
        """Nodes to try, preferred first, breaker-open ones last.

        Open-breaker nodes are kept at the end rather than removed so that a
        total outage still produces a real connection error (and a PARTITIONED
        classification) instead of a confusing "no candidates" state.
        """
        with self._pref_lock:
            start = self._preferred
        ordered = [self._nodes[(start + i) % len(self._nodes)] for i in range(len(self._nodes))]
        healthy = [n for n in ordered if n.breaker.allows_request()]
        blocked = [n for n in ordered if not n.breaker.allows_request()]
        return healthy + blocked

    def close(self) -> None:
        for node in self._nodes:
            self._drop_channel(node)


# --- the retry loop -----------------------------------------------------------


def _backoff_delay(attempt: int, cfg: RetryConfig) -> float:
    """Exponential backoff with full jitter (AWS-style).

    Full jitter rather than fixed backoff so that many simulator clients that
    lose a node simultaneously do not retry in lockstep and thunder the
    replacement.
    """
    ceiling = min(cfg.max_backoff_s, cfg.initial_backoff_s * (cfg.multiplier**attempt))
    return random.uniform(0, ceiling) if cfg.jitter else ceiling


def call(
    pool: NodePool,
    policy: RetryPolicy,
    invoke: Callable[[object, str], T],
    *,
    config: ClientConfig | None = None,
    on_retry: Callable[[AuctionError, int, float], None] | None = None,
) -> T:
    """Run ``invoke(stub, endpoint)`` with retry, backoff and failover.

    ``invoke`` must perform exactly one RPC and return its reply. Raises the
    final ``AuctionError`` when every attempt is exhausted.
    """
    cfg = config or CONFIG
    max_attempts = policy.max_attempts or cfg.retry.max_attempts

    last: AuctionError | None = None
    attempt = 0

    while attempt < max_attempts:
        node = pool.candidates()[0]
        stub = pool._stub_for(node)

        try:
            result = invoke(stub, node.endpoint)
        except grpc.RpcError as exc:
            err = classify_rpc_error(exc, endpoint=node.endpoint)
        except AuctionError as exc:  # raised by the caller's own validation
            err = exc
        else:
            node.breaker.record_success()
            pool.prefer(node.endpoint)
            return result

        last = err
        node.breaker.record_failure()

        # A dead connection must not be reused; force a fresh one next time.
        if err.kind in (ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT):
            pool._drop_channel(node)

        # A leader hint is the fastest possible redirect; honour it before
        # falling back to round-robin. (Milestone 2 -- no server emits this yet.)
        if err.leader_hint:
            pool.prefer(err.leader_hint)
        elif err.kind.should_failover and len(pool.endpoints) > 1:
            pool._advance()

        may_retry = (policy.retry_on_retryable and err.retryable) or (
            policy.retry_on_never_sent and _never_reached_server(err)
        )
        attempt += 1
        if not may_retry or attempt >= max_attempts:
            break

        delay = _backoff_delay(attempt, cfg.retry)
        log.warning(
            "%s failed on %s (%s); retry %d/%d in %.2fs",
            policy.name,
            node.endpoint,
            err.kind.value,
            attempt,
            max_attempts,
            delay,
        )
        if on_retry:
            on_retry(err, attempt, delay)
        time.sleep(delay)

    assert last is not None
    # Every endpoint being down is a partition, not a plain per-node outage --
    # a materially different thing to show the user.
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
