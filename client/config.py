"""Client configuration. Most values can be overridden by env var."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name, default):
    return os.environ.get(name, default)


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _default_endpoints():
    raw = os.environ.get("AUCTION_ENDPOINTS")
    if raw:
        return [e.strip() for e in raw.split(",") if e.strip()]

    host = _env_str("APP_HOST", "localhost")
    port = _env_int("APP_PORT", 50051)

    # .env.example has APP_HOST=0.0.0.0, which is a bind address, not something
    # you can dial. Rewrite it so a copied .env doesn't break the client.
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"

    return [f"{host}:{port}"]


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = field(default_factory=lambda: _env_int("AUCTION_RETRY_ATTEMPTS", 4))
    initial_backoff_s: float = field(default_factory=lambda: _env_float("AUCTION_RETRY_BACKOFF", 0.15))
    max_backoff_s: float = field(default_factory=lambda: _env_float("AUCTION_RETRY_MAX_BACKOFF", 3.0))
    multiplier: float = 2.0
    jitter: float = 1.0


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = field(default_factory=lambda: _env_int("AUCTION_BREAKER_THRESHOLD", 3))
    reset_timeout_s: float = field(default_factory=lambda: _env_float("AUCTION_BREAKER_RESET", 5.0))
    success_threshold: int = 2


@dataclass(frozen=True)
class WatchConfig:
    fast_interval_s: float = field(default_factory=lambda: _env_float("AUCTION_POLL_FAST", 0.5))
    idle_interval_s: float = field(default_factory=lambda: _env_float("AUCTION_POLL_IDLE", 3.0))
    urgent_window_s: float = 30.0
    activity_window_s: float = 10.0


@dataclass(frozen=True)
class ClientConfig:
    endpoints: list[str] = field(default_factory=_default_endpoints)
    rpc_timeout_s: float = field(default_factory=lambda: _env_float("AUCTION_RPC_TIMEOUT", 5.0))
    # Local model generation is far slower than an auction RPC.
    llm_timeout_s: float = field(default_factory=lambda: _env_float("AUCTION_LLM_TIMEOUT", 90.0))
    retry: RetryConfig = field(default_factory=RetryConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    auto_reauth: bool = True

    def grpc_channel_options(self):
        # Keepalive has to stay inside what the server tolerates. grpc servers
        # accept roughly one ping per 5 min while they aren't sending data, and
        # AskLLM holds the call open for a minute or more while the model
        # generates. Pinging every 10s through that earned a GOAWAY with
        # ENHANCE_YOUR_CALM and killed the connection mid-answer.
        #
        # Liveness comes from per-call deadlines instead -- a dead node fails
        # on connect, a hung one hits rpc_timeout_s.
        return [
            ("grpc.keepalive_time_ms", 300_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 0),
            ("grpc.http2.max_pings_without_data", 2),
            ("grpc.enable_retries", 0),
        ]


CONFIG = ClientConfig()
