"""Client-side configuration.

Every knob is overridable by environment variable so the demo can be retuned
without editing code (useful when showing failover on 5 nodes).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _default_endpoints() -> list[str]:
    """Application-server endpoints, highest preference first.

    Milestone 1 runs a single app server, so the default is one entry. Point
    AUCTION_ENDPOINTS at a comma-separated list for Milestone 2 -- the node
    pool already fails over across whatever it is given.
    """
    raw = os.environ.get("AUCTION_ENDPOINTS")
    if raw:
        return [e.strip() for e in raw.split(",") if e.strip()]
    host = _env_str("APP_HOST", "localhost")
    port = _env_int("APP_PORT", 50051)
    # .env.example ships APP_HOST=0.0.0.0, which is a bind address, not a
    # dial address. Rewrite it so a copied .env does not break the client.
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"
    return [f"{host}:{port}"]


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = field(default_factory=lambda: _env_int("AUCTION_RETRY_ATTEMPTS", 4))
    initial_backoff_s: float = field(default_factory=lambda: _env_float("AUCTION_RETRY_BACKOFF", 0.15))
    max_backoff_s: float = field(default_factory=lambda: _env_float("AUCTION_RETRY_MAX_BACKOFF", 3.0))
    multiplier: float = 2.0
    # Full jitter. Without it, N simulator clients that lose the leader
    # together all retry on the same schedule and re-thunder the new one.
    jitter: float = 1.0


@dataclass(frozen=True)
class BreakerConfig:
    # Consecutive transport failures before an endpoint is taken out of
    # rotation. Low enough to fail over fast during the leader-kill demo.
    failure_threshold: int = field(default_factory=lambda: _env_int("AUCTION_BREAKER_THRESHOLD", 3))
    # How long an open breaker waits before allowing one probe through.
    reset_timeout_s: float = field(default_factory=lambda: _env_float("AUCTION_BREAKER_RESET", 5.0))
    # Consecutive probe successes needed to fully close again.
    success_threshold: int = 2


@dataclass(frozen=True)
class WatchConfig:
    """Polling cadence for the change watcher.

    proto/auction.proto has no server-streaming RPC, so real-time updates are
    synthesised by polling. The interval adapts: auctions closing soon are
    polled hard, idle ones slowly, which keeps the demo responsive without
    hammering a single-threaded server.
    """

    fast_interval_s: float = field(default_factory=lambda: _env_float("AUCTION_POLL_FAST", 0.5))
    idle_interval_s: float = field(default_factory=lambda: _env_float("AUCTION_POLL_IDLE", 3.0))
    # An auction ending within this many seconds is polled at fast_interval.
    urgent_window_s: float = 30.0
    # Keep polling fast for this long after any observed change.
    activity_window_s: float = 10.0


@dataclass(frozen=True)
class ClientConfig:
    endpoints: list[str] = field(default_factory=_default_endpoints)
    rpc_timeout_s: float = field(default_factory=lambda: _env_float("AUCTION_RPC_TIMEOUT", 5.0))
    retry: RetryConfig = field(default_factory=RetryConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)

    # Re-login automatically when the server reports the token is invalid.
    # Requires credentials to have been supplied to SessionManager.
    auto_reauth: bool = True

    def grpc_channel_options(self) -> list[tuple[str, int]]:
        """Keepalive so a partitioned TCP connection is detected in seconds.

        Without these a client blocked on a dead node can hang until the OS
        TCP timeout (minutes), which would make the leader-failure demo look
        like a client freeze.
        """
        return [
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 3_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
            ("grpc.enable_retries", 0),  # we retry in resilience.py, not in grpc-core
        ]


CONFIG = ClientConfig()
