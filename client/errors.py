"""Error types for the client.

auction.proto has no error codes, so failures arrive either as a raw
grpc.RpcError or as StatusResponse(success=false, message="some prose").
Both get funnelled through here into an AuctionError with an ErrorKind, so
nothing outside this module has to match on message strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import grpc


class ErrorKind(str, Enum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    PARTITIONED = "partitioned"

    # Raft-specific; the server does not emit these yet.
    NOT_LEADER = "not_leader"
    NO_QUORUM = "no_quorum"
    CONSENSUS_TIMEOUT = "consensus_timeout"

    UNAUTHENTICATED = "unauthenticated"

    NOT_FOUND = "not_found"
    BID_TOO_LOW = "bid_too_low"
    AUCTION_NOT_ACTIVE = "auction_not_active"
    INVALID_ARGUMENT = "invalid_argument"

    SERVER_BUG = "server_bug"
    UNKNOWN = "unknown"

    @property
    def retryable(self):
        return self in _RETRYABLE

    @property
    def should_failover(self):
        return self in _FAILOVER


# SERVER_BUG is deliberately excluded: a servicer exception is deterministic,
# so retrying just multiplies load.
_RETRYABLE = frozenset({
    ErrorKind.UNAVAILABLE,
    ErrorKind.TIMEOUT,
    ErrorKind.PARTITIONED,
    ErrorKind.NOT_LEADER,
    ErrorKind.NO_QUORUM,
    ErrorKind.CONSENSUS_TIMEOUT,
})

_FAILOVER = frozenset({
    ErrorKind.UNAVAILABLE,
    ErrorKind.TIMEOUT,
    ErrorKind.PARTITIONED,
    ErrorKind.NOT_LEADER,
})


class AuctionError(Exception):

    def __init__(self, kind, message, endpoint=None, attempts=1, cause=None, leader_hint=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.endpoint = endpoint
        self.attempts = attempts
        self.cause = cause
        self.leader_hint = leader_hint

    @property
    def retryable(self):
        return self.kind.retryable

    def __str__(self):
        where = f" [{self.endpoint}]" if self.endpoint else ""
        tries = f" after {self.attempts} attempts" if self.attempts > 1 else ""
        return f"{self.kind.value}{where}{tries}: {self.message}"

    def user_message(self):
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


def classify_rpc_error(exc, endpoint=None):
    code = exc.code() if hasattr(exc, "code") else grpc.StatusCode.UNKNOWN
    detail = (exc.details() if hasattr(exc, "details") else None) or str(exc)

    kind = _GRPC_KIND.get(code, ErrorKind.UNKNOWN)

    # A servicer that raised is deterministic, so retrying it is pointless.
    if code == grpc.StatusCode.UNKNOWN and "Exception calling application" in detail:
        kind = ErrorKind.SERVER_BUG

    return AuctionError(kind, detail, endpoint=endpoint, cause=exc)


# Matching on prose is fragile, so it's confined to this table. Every pattern
# corresponds to a literal in application/auction_manager.py or grpc_service.py.
_MESSAGE_PATTERNS = (
    (re.compile(r"not authenticated", re.I), ErrorKind.UNAUTHENTICATED),
    (re.compile(r"invalid credentials", re.I), ErrorKind.UNAUTHENTICATED),
    (re.compile(r"auction not found", re.I), ErrorKind.NOT_FOUND),
    (re.compile(r"higher than the current highest bid", re.I), ErrorKind.BID_TOO_LOW),
    (re.compile(r"auction has ended", re.I), ErrorKind.AUCTION_NOT_ACTIVE),
    (re.compile(r"auction is not active", re.I), ErrorKind.AUCTION_NOT_ACTIVE),
    (re.compile(r"cannot be negative", re.I), ErrorKind.INVALID_ARGUMENT),
    (re.compile(r"must be positive", re.I), ErrorKind.INVALID_ARGUMENT),
    (re.compile(r"no valid (operation|query) specified", re.I), ErrorKind.INVALID_ARGUMENT),
    # Raft-specific.
    (re.compile(r"not (the )?leader", re.I), ErrorKind.NOT_LEADER),
    (re.compile(r"no quorum|lost quorum", re.I), ErrorKind.NO_QUORUM),
    (re.compile(r"consensus timeout|commit timeout", re.I), ErrorKind.CONSENSUS_TIMEOUT),
)

_LEADER_HINT = re.compile(r"leader(?: is)?[:= ]+([\w.\-]+:\d+)", re.I)


def classify_status(message, endpoint=None):
    text = (message or "").strip() or "request failed"

    for pattern, kind in _MESSAGE_PATTERNS:
        if pattern.search(text):
            hint = _LEADER_HINT.search(text)
            return AuctionError(
                kind,
                text,
                endpoint=endpoint,
                leader_hint=hint.group(1) if hint else None,
            )

    return AuctionError(ErrorKind.UNKNOWN, text, endpoint=endpoint)
