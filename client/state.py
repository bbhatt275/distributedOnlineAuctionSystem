"""Shared client state, with change notification.

Used by the CLI, the web app and the simulator. Everything goes in through the
apply_* methods, which diff against what's already held and emit events for
whatever actually changed.

Auction has no version field, so ordering relies on invariants the server
already guarantees: the highest bid never drops while an auction is active,
and auctions never reopen once closed. Anything contradicting those is treated
as a stale read and dropped.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("auction.client.state")


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    PARTITIONED = "partitioned"


class EventType(str, Enum):
    AUCTION_ADDED = "auction_added"
    AUCTION_UPDATED = "auction_updated"
    AUCTION_CLOSED = "auction_closed"
    BID_PLACED = "bid_placed"
    OUTBID = "outbid"
    SESSION_CHANGED = "session_changed"
    CONNECTION_CHANGED = "connection_changed"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    type: EventType
    payload: dict = field(default_factory=dict)


@dataclass
class Session:
    username: str | None = None
    token: str | None = None

    @property
    def authenticated(self):
        return bool(self.token)


@dataclass
class AuctionView:
    """Plain mirror of the protobuf Auction, so templates and diffing never
    touch generated code."""

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
    def from_proto(cls, msg):
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

    def seconds_remaining(self, now):
        return max(0, int(self.end_time - now))

    def has_bids(self):
        # The current highest bid is seeded to the starting price, so it
        # cannot indicate on its own whether anyone has bid.
        return bool(self.highest_bidder)


@dataclass
class BidView:
    bid_id: str
    auction_id: str
    bidder: str
    amount: float
    timestamp: int

    @classmethod
    def from_proto(cls, msg):
        return cls(
            bid_id=msg.bid_id,
            auction_id=msg.auction_id,
            bidder=msg.bidder,
            amount=msg.amount,
            timestamp=msg.timestamp,
        )


_TRACKED = ("current_highest_bid", "highest_bidder", "active", "winner",
            "description", "item_name")


class Store:

    def __init__(self):
        self._lock = threading.RLock()
        self._session = Session()
        self._auctions = {}
        self._bids = {}
        self._connection = ConnectionState.DISCONNECTED
        self._last_error = None
        self._subscribers = []

    def subscribe(self, callback):
        """Register a listener. Returns a function that removes it again."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, events):
        events = list(events)
        if not events:
            return

        with self._lock:
            listeners = list(self._subscribers)

        # Events are dispatched outside the lock, because a subscriber such as
        # the command line renderer reads from the store while handling one.
        for event in events:
            for listener in listeners:
                try:
                    listener(event)
                except Exception:
                    log.exception("subscriber raised on %s", event.type.value)

    @property
    def session(self):
        with self._lock:
            return Session(self._session.username, self._session.token)

    @property
    def connection(self):
        with self._lock:
            return self._connection

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def auctions(self, active_only=False):
        with self._lock:
            items = list(self._auctions.values())

        if active_only:
            items = [a for a in items if a.active]

        return sorted(items, key=lambda a: (not a.active, a.end_time))

    def auction(self, auction_id):
        with self._lock:
            return self._auctions.get(auction_id)

    def bids(self, auction_id):
        with self._lock:
            found = list(self._bids.get(auction_id, {}).values())

        # Bids are ordered by amount rather than by time. Timestamps have
        # whole second resolution, so simultaneous bids share one, whereas
        # amounts are strictly increasing and therefore order them reliably.
        return sorted(found, key=lambda b: b.amount, reverse=True)

    def set_session(self, username, token):
        with self._lock:
            self._session = Session(username, token)
        self._emit([Event(EventType.SESSION_CHANGED,
                          {"username": username, "authenticated": bool(token)})])

    def clear_session(self):
        with self._lock:
            self._session = Session()
            self._auctions.clear()
            self._bids.clear()
        self._emit([Event(EventType.SESSION_CHANGED,
                          {"username": None, "authenticated": False})])

    def set_connection(self, state, detail=None):
        with self._lock:
            if self._connection == state and detail == self._last_error:
                return
            self._connection = state
            self._last_error = detail
        self._emit([Event(EventType.CONNECTION_CHANGED,
                          {"state": state.value, "detail": detail})])

    def record_error(self, message):
        with self._lock:
            self._last_error = message
        self._emit([Event(EventType.ERROR, {"message": message})])

    def apply_auctions(self, incoming):
        events = []
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
                    events.append(Event(EventType.AUCTION_UPDATED,
                                        {"auction": view, "changed": changed}))

                if ("highest_bidder" in changed and was_leader
                        and view.highest_bidder and view.highest_bidder != me):
                    events.append(Event(EventType.OUTBID, {
                        "auction": view,
                        "by": view.highest_bidder,
                        "amount": view.current_highest_bid,
                    }))

        self._emit(events)
        return events

    @staticmethod
    def _is_stale(existing, incoming):
        if not existing.active and incoming.active:
            return True
        if (existing.active and incoming.active
                and incoming.current_highest_bid < existing.current_highest_bid):
            return True
        return False

    def apply_bids(self, auction_id, incoming):
        events = []

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

    def snapshot(self):
        """Serialisable view of everything, for the web UI's first render."""
        with self._lock:
            return {
                "connection": self._connection.value,
                "username": self._session.username,
                "authenticated": self._session.authenticated,
                "last_error": self._last_error,
                "auctions": [vars(a) for a in self.auctions()],
            }
