"""Observable client-side state store.

A single source of truth for the CLI, the web UI and the load simulator. All
mutation goes through ``apply_*`` methods which diff incoming server snapshots
against what is already held and emit typed ``Event`` objects describing what
actually changed. Subscribers (terminal renderer, SSE stream) react to events
rather than re-rendering blindly.

Ordering without a version field
--------------------------------
proto/auction.proto's ``Auction`` has no version or sequence number, so the
store cannot simply compare versions to reject stale data. Instead it relies
on the invariants the server actually guarantees:

* ``current_highest_bid`` is monotonically non-decreasing while an auction is
  active (application/auction_manager.py rejects ``amount <= current``), so a
  snapshot proposing a *lower* highest bid on a still-active auction is stale
  and is dropped.
* ``active`` only ever goes True -> False, never back, so a snapshot trying to
  reopen a closed auction is stale and is dropped.
* Bids are identified by ``bid_id`` (a uuid4), so new bids are found by set
  difference rather than by comparing counts or timestamps.

The last point matters because ``Bid.timestamp`` is whole seconds
(``int(time.time())``), which is far too coarse to order the concurrent bids
the demo deliberately produces. Timestamps are shown to users but never used
to decide ordering. See docs/proto-gaps.md.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("auction.client.state")


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"      # reachable, but retries are happening
    PARTITIONED = "partitioned"  # no endpoint reachable


class EventType(str, Enum):
    AUCTION_ADDED = "auction_added"
    AUCTION_UPDATED = "auction_updated"
    AUCTION_CLOSED = "auction_closed"
    BID_PLACED = "bid_placed"
    OUTBID = "outbid"                 # this user was the leader and no longer is
    SESSION_CHANGED = "session_changed"
    CONNECTION_CHANGED = "connection_changed"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    username: str | None = None
    token: str | None = None

    @property
    def authenticated(self) -> bool:
        return bool(self.token)


@dataclass
class AuctionView:
    """Plain-Python mirror of a protobuf ``Auction``.

    Kept as a dataclass rather than holding the protobuf object so the web
    templates and the diffing logic never touch generated code, and so a proto
    change is absorbed in ``from_proto`` alone.
    """

    auction_id: str
    item_name: str
    description: str
    starting_price: float
    current_highest_bid: float
    highest_bidder: str
    start_time: int
    end_time: int
    active: bool
    winner: str

    @classmethod
    def from_proto(cls, msg) -> "AuctionView":
        return cls(
            auction_id=msg.auction_id,
            item_name=msg.item_name,
            description=msg.description,
            starting_price=msg.starting_price,
            current_highest_bid=msg.current_highest_bid,
            highest_bidder=msg.highest_bidder,
            start_time=msg.start_time,
            end_time=msg.end_time,
            active=msg.active,
            winner=msg.winner,
        )

    def seconds_remaining(self, now: float) -> int:
        return max(0, int(self.end_time - now))

    def has_bids(self) -> bool:
        """A fresh auction seeds current_highest_bid with starting_price."""
        return bool(self.highest_bidder)


@dataclass
class BidView:
    bid_id: str
    auction_id: str
    bidder: str
    amount: float
    timestamp: int

    @classmethod
    def from_proto(cls, msg) -> "BidView":
        return cls(
            bid_id=msg.bid_id,
            auction_id=msg.auction_id,
            bidder=msg.bidder,
            amount=msg.amount,
            timestamp=msg.timestamp,
        )


# Fields whose change is worth telling the UI about.
_TRACKED = ("current_highest_bid", "highest_bidder", "active", "winner", "description", "item_name")


class Store:
    """Thread-safe observable state container."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = Session()
        self._auctions: dict[str, AuctionView] = {}
        self._bids: dict[str, dict[str, BidView]] = {}
        self._connection = ConnectionState.DISCONNECTED
        self._last_error: str | None = None
        self._subscribers: list[Callable[[Event], None]] = []

    # -- subscription ---------------------------------------------------------

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Register a listener. Returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, events: Iterable[Event]) -> None:
        events = list(events)
        if not events:
            return
        with self._lock:
            listeners = list(self._subscribers)
        # Dispatched outside the lock: a subscriber that calls back into the
        # store (the CLI renderer does) would otherwise re-enter while held.
        for event in events:
            for listener in listeners:
                try:
                    listener(event)
                except Exception:
                    log.exception("subscriber raised on %s", event.type.value)

    # -- reads ----------------------------------------------------------------

    @property
    def session(self) -> Session:
        with self._lock:
            return Session(self._session.username, self._session.token)

    @property
    def connection(self) -> ConnectionState:
        with self._lock:
            return self._connection

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def auctions(self, *, active_only: bool = False) -> list[AuctionView]:
        with self._lock:
            items = list(self._auctions.values())
        if active_only:
            items = [a for a in items if a.active]
        # Active first, then soonest to end -- the order the UI wants.
        return sorted(items, key=lambda a: (not a.active, a.end_time))

    def auction(self, auction_id: str) -> AuctionView | None:
        with self._lock:
            return self._auctions.get(auction_id)

    def bids(self, auction_id: str) -> list[BidView]:
        with self._lock:
            found = list(self._bids.get(auction_id, {}).values())
        # Amount descending is a total order here (the server enforces strictly
        # increasing bids), unlike the second-resolution timestamp.
        return sorted(found, key=lambda b: b.amount, reverse=True)

    # -- session --------------------------------------------------------------

    def set_session(self, username: str | None, token: str | None) -> None:
        with self._lock:
            self._session = Session(username, token)
        self._emit([Event(EventType.SESSION_CHANGED, {"username": username, "authenticated": bool(token)})])

    def clear_session(self) -> None:
        with self._lock:
            self._session = Session()
            # Auction data is per-user visible state; drop it on logout so a
            # second login in the same process starts clean.
            self._auctions.clear()
            self._bids.clear()
        self._emit([Event(EventType.SESSION_CHANGED, {"username": None, "authenticated": False})])

    # -- connection -----------------------------------------------------------

    def set_connection(self, state: ConnectionState, detail: str | None = None) -> None:
        with self._lock:
            if self._connection == state and detail == self._last_error:
                return
            self._connection = state
            self._last_error = detail
        self._emit([Event(EventType.CONNECTION_CHANGED, {"state": state.value, "detail": detail})])

    def record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self._emit([Event(EventType.ERROR, {"message": message})])

    # -- auction ingestion ----------------------------------------------------

    def apply_auctions(self, incoming: Iterable[Any]) -> list[Event]:
        """Merge a batch of protobuf Auctions, emitting only real changes."""
        events: list[Event] = []
        me = self.session.username

        with self._lock:
            for msg in incoming:
                view = msg if isinstance(msg, AuctionView) else AuctionView.from_proto(msg)
                existing = self._auctions.get(view.auction_id)

                if existing is None:
                    self._auctions[view.auction_id] = view
                    events.append(Event(EventType.AUCTION_ADDED, {"auction": view}))
                    continue

                if self._is_stale(existing, view):
                    continue

                changed = [f for f in _TRACKED if getattr(existing, f) != getattr(view, f)]
                if not changed:
                    continue

                was_leader = bool(me) and existing.highest_bidder == me
                self._auctions[view.auction_id] = view

                if existing.active and not view.active:
                    events.append(Event(EventType.AUCTION_CLOSED, {"auction": view}))
                else:
                    events.append(
                        Event(EventType.AUCTION_UPDATED, {"auction": view, "changed": changed})
                    )

                if (
                    "highest_bidder" in changed
                    and was_leader
                    and view.highest_bidder != me
                    and view.highest_bidder
                ):
                    events.append(
                        Event(
                            EventType.OUTBID,
                            {"auction": view, "by": view.highest_bidder, "amount": view.current_highest_bid},
                        )
                    )

        self._emit(events)
        return events

    @staticmethod
    def _is_stale(existing: AuctionView, incoming: AuctionView) -> bool:
        """Reject snapshots that contradict the server's own invariants.

        Guards against out-of-order replies from concurrent pollers, and
        against a stale read from a lagging follower once Raft lands.
        """
        if not existing.active and incoming.active:
            return True  # auctions never reopen
        if (
            existing.active
            and incoming.active
            and incoming.current_highest_bid < existing.current_highest_bid
        ):
            return True  # highest bid never decreases while active
        return False

    def apply_bids(self, auction_id: str, incoming: Iterable[Any]) -> list[Event]:
        """Merge the bid list for one auction; emits only genuinely new bids."""
        events: list[Event] = []

        with self._lock:
            known = self._bids.setdefault(auction_id, {})
            for msg in incoming:
                view = msg if isinstance(msg, BidView) else BidView.from_proto(msg)
                if view.bid_id in known:
                    continue
                known[view.bid_id] = view
                events.append(Event(EventType.BID_PLACED, {"bid": view}))

        self._emit(events)
        return events

    def snapshot(self) -> dict[str, Any]:
        """Serialisable view of everything, for the web UI's initial render."""
        with self._lock:
            return {
                "connection": self._connection.value,
                "username": self._session.username,
                "authenticated": self._session.authenticated,
                "last_error": self._last_error,
                "auctions": [vars(a) for a in self.auctions()],
            }
