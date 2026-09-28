"""Typed client-side error taxonomy.

proto/auction.proto carries no error codes -- every failure arrives as
``StatusResponse{success=false, message="some prose"}`` or as a raw
``grpc.RpcError``. The rest of the client must not branch on prose, so
everything funnels through here and comes out as an ``AuctionError`` with a
machine-readable ``ErrorKind``.

When the proto eventually grows a StatusCode enum (see docs/proto-gaps.md),
only ``classify_status`` changes; callers stay as they are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import grpc


class ErrorKind(str, Enum):
    # --- transport / availability -------------------------------------------
    UNAVAILABLE = "unavailable"          # node down, refused, or unreachable
    TIMEOUT = "timeout"                  # deadline exceeded
    PARTITIONED = "partitioned"          # every known endpoint is unreachable
    # --- consensus (Milestone 2; classified now so the UI is ready) ----------
    NOT_LEADER = "not_leader"            # write sent to a follower
    NO_QUORUM = "no_quorum"              # leader cannot commit, lost majority
    CONSENSUS_TIMEOUT = "consensus_timeout"
    # --- auth ----------------------------------------------------------------
    UNAUTHENTICATED = "unauthenticated"  # bad/expired token, bad credentials
    # --- domain --------------------------------------------------------------
    NOT_FOUND = "not_found"
    BID_TOO_LOW = "bid_too_low"
    AUCTION_NOT_ACTIVE = "auction_not_active"
    INVALID_ARGUMENT = "invalid_argument"
    # --- other ---------------------------------------------------------------
    SERVER_BUG = "server_bug"            # server raised; retrying will not help
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        """Whether retrying the *same* call could plausibly succeed.

        Deliberately excludes SERVER_BUG: a server-side exception is
        deterministic, so retrying just multiplies load during a demo.
        """
        return self in _RETRYABLE

    @property
    def should_failover(self) -> bool:
        """Whether to try a different endpoint rather than the same one."""
        return self in _FAILOVER


_RETRYABLE = frozenset(
    {
        ErrorKind.UNAVAILABLE,
        ErrorKind.TIMEOUT,
        ErrorKind.PARTITIONED,
        ErrorKind.NOT_LEADER,
        ErrorKind.NO_QUORUM,
        ErrorKind.CONSENSUS_TIMEOUT,
    }
)

_FAILOVER = frozenset(
    {
        ErrorKind.UNAVAILABLE,
        ErrorKind.TIMEOUT,
        ErrorKind.PARTITIONED,
        ErrorKind.NOT_LEADER,
    }
)


class AuctionError(Exception):
    """A classified client-facing failure."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        endpoint: str | None = None,
        attempts: int = 1,
        cause: BaseException | None = None,
        leader_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.endpoint = endpoint
        self.attempts = attempts
        self.cause = cause
        self.leader_hint = leader_hint

    @property
    def retryable(self) -> bool:
        return self.kind.retryable

    def __str__(self) -> str:
        where = f" [{self.endpoint}]" if self.endpoint else ""
        tries = f" after {self.attempts} attempts" if self.attempts > 1 else ""
        return f"{self.kind.value}{where}{tries}: {self.message}"

    def user_message(self) -> str:
        """Prose suitable for the CLI or a web flash message."""
        return _USER_MESSAGES.get(self.kind, self.message)


_USER_MESSAGES = {
    ErrorKind.UNAVAILABLE: "Auction server is unreachable. Retrying in the background.",
    ErrorKind.TIMEOUT: "The server took too long to respond. Please try again.",
    ErrorKind.PARTITIONED: "Lost contact with every auction server. Check the network.",
    ErrorKind.NOT_LEADER: "Cluster is electing a new leader. Your request will be retried.",
    ErrorKind.NO_QUORUM: "The cluster has lost quorum and cannot accept writes right now.",
    ErrorKind.CONSENSUS_TIMEOUT: "The cluster could not confirm your request in time.",
    ErrorKind.UNAUTHENTICATED: "Your session has expired. Please log in again.",
    ErrorKind.SERVER_BUG: "The server hit an internal error. This is a bug, not your input.",
}


# --- gRPC transport classification -------------------------------------------

_GRPC_KIND = {
    grpc.StatusCode.UNAVAILABLE: ErrorKind.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED: ErrorKind.TIMEOUT,
    grpc.StatusCode.UNAUTHENTICATED: ErrorKind.UNAUTHENTICATED,
    grpc.StatusCode.PERMISSION_DENIED: ErrorKind.UNAUTHENTICATED,
    grpc.StatusCode.NOT_FOUND: ErrorKind.NOT_FOUND,
    grpc.StatusCode.INVALID_ARGUMENT: ErrorKind.INVALID_ARGUMENT,
    grpc.StatusCode.RESOURCE_EXHAUSTED: ErrorKind.UNAVAILABLE,
    grpc.StatusCode.ABORTED: ErrorKind.CONSENSUS_TIMEOUT,
    grpc.StatusCode.FAILED_PRECONDITION: ErrorKind.NOT_LEADER,
}


def classify_rpc_error(exc: grpc.RpcError, *, endpoint: str | None = None) -> AuctionError:
    """Map a raw gRPC transport failure onto an AuctionError."""
    code = exc.code() if hasattr(exc, "code") else grpc.StatusCode.UNKNOWN
    detail = (exc.details() if hasattr(exc, "details") else None) or str(exc)

    kind = _GRPC_KIND.get(code, ErrorKind.UNKNOWN)

    # A servicer that raised an exception surfaces as UNKNOWN with
    # "Exception calling application: ...". That is deterministic, so mark it
    # SERVER_BUG rather than retrying it.
    #
    # This is not hypothetical: application/grpc_service.py Post() constructs
    # StatusResponse(mesage=...) on the unauthenticated path (typo for
    # "message"), so every Post with a stale token lands here instead of
    # returning success=False. Tracked in docs/server-issues.md.
    if code == grpc.StatusCode.UNKNOWN and "Exception calling application" in detail:
        if "no \"mesage\" field" in detail or 'has no "mesage"' in detail:
            return AuctionError(
                ErrorKind.UNAUTHENTICATED,
                "Session rejected by server (server-side typo makes this surface as a crash)",
                endpoint=endpoint,
                cause=exc,
            )
        kind = ErrorKind.SERVER_BUG

    return AuctionError(kind, detail, endpoint=endpoint, cause=exc)


# --- StatusResponse message classification -----------------------------------
#
# Matching on prose is brittle by nature. It is contained here on purpose, and
# every pattern is anchored to a literal string in application/auction_manager.py
# or application/grpc_service.py. If the server reworks its messages, only this
# table needs updating -- and docs/proto-gaps.md proposes replacing it outright
# with a StatusCode enum.

_MESSAGE_PATTERNS: tuple[tuple[re.Pattern[str], ErrorKind], ...] = (
    (re.compile(r"not authenticated", re.I), ErrorKind.UNAUTHENTICATED),
    (re.compile(r"invalid credentials", re.I), ErrorKind.UNAUTHENTICATED),
    (re.compile(r"auction not found", re.I), ErrorKind.NOT_FOUND),
    (re.compile(r"higher than the current highest bid", re.I), ErrorKind.BID_TOO_LOW),
    (re.compile(r"auction has ended", re.I), ErrorKind.AUCTION_NOT_ACTIVE),
    (re.compile(r"auction is not active", re.I), ErrorKind.AUCTION_NOT_ACTIVE),
    (re.compile(r"cannot be negative", re.I), ErrorKind.INVALID_ARGUMENT),
    (re.compile(r"must be positive", re.I), ErrorKind.INVALID_ARGUMENT),
    (re.compile(r"no valid (operation|query) specified", re.I), ErrorKind.INVALID_ARGUMENT),
    # --- Milestone 2 shapes, matched ahead of the server implementing them ---
    (re.compile(r"not (the )?leader", re.I), ErrorKind.NOT_LEADER),
    (re.compile(r"no quorum|lost quorum", re.I), ErrorKind.NO_QUORUM),
    (re.compile(r"consensus timeout|commit timeout", re.I), ErrorKind.CONSENSUS_TIMEOUT),
)

_LEADER_HINT = re.compile(r"leader(?: is)?[:= ]+([\w.\-]+:\d+)", re.I)


def classify_status(message: str, *, endpoint: str | None = None) -> AuctionError:
    """Map a ``StatusResponse.message`` from a ``success=False`` reply."""
    text = (message or "").strip() or "request failed"
    for pattern, kind in _MESSAGE_PATTERNS:
        if pattern.search(text):
            hint_match = _LEADER_HINT.search(text)
            return AuctionError(
                kind,
                text,
                endpoint=endpoint,
                leader_hint=hint_match.group(1) if hint_match else None,
            )
    return AuctionError(ErrorKind.UNKNOWN, text, endpoint=endpoint)
