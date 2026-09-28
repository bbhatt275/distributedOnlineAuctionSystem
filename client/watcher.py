"""Real-time auction updates.

proto/auction.proto exposes no server-streaming RPC, so "real-time bid
placement" is synthesised client-side: a background thread polls and the
``Store`` diffs each snapshot into ``BID_PLACED`` / ``AUCTION_UPDATED`` /
``OUTBID`` / ``AUCTION_CLOSED`` events. Subscribers cannot tell the difference
between this and a real stream, which is the point -- when the server grows a
``WatchAuctions`` streaming RPC (docs/proto-gaps.md) only this file changes.

The interval adapts so the demo stays responsive without hammering the
server's 10-worker thread pool:

* an auction ending within ``urgent_window_s``      -> fast interval
* anything changed within ``activity_window_s``     -> fast interval
* otherwise                                         -> idle interval

Backoff on failure is handled underneath by ``client.resilience``; the watcher
additionally stops polling bid detail while the connection is partitioned, so
a dead cluster does not produce a retry storm from every open browser tab.
"""

from __future__ import annotations

import logging
import threading
import time

from client.auction_client import AuctionClient
from client.config import CONFIG, WatchConfig
from client.errors import AuctionError, ErrorKind
from client.state import ConnectionState, EventType

log = logging.getLogger("auction.client.watcher")


class AuctionWatcher:
    """Background poller that keeps the Store fresh."""

    def __init__(
        self,
        client: AuctionClient,
        *,
        config: WatchConfig | None = None,
        track_bids: bool = True,
    ) -> None:
        self._client = client
        self._config = config or CONFIG.watch
        self._track_bids = track_bids
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_change = 0.0
        self._consecutive_failures = 0
        # Auctions whose bid list we poll in detail. Polling bids for every
        # auction is O(auctions) RPCs per tick, so detail is opt-in: the UI
        # registers whichever auction the user is actually looking at.
        self._focus: set[str] = set()
        self._focus_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auction-watcher", daemon=True)
        self._thread.start()
        log.info("watcher started (fast=%.1fs idle=%.1fs)", self._config.fast_interval_s, self._config.idle_interval_s)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("watcher stopped")

    def __enter__(self) -> "AuctionWatcher":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- focus ----------------------------------------------------------------

    def focus(self, auction_id: str) -> None:
        """Poll this auction's bid list in detail."""
        with self._focus_lock:
            self._focus.add(auction_id)
        self.refresh_now()

    def unfocus(self, auction_id: str) -> None:
        with self._focus_lock:
            self._focus.discard(auction_id)

    def refresh_now(self) -> None:
        """Wake the loop immediately, e.g. right after the user bids."""
        self._wake.set()

    # -- loop -----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            interval = self._tick()
            # Event-based sleep so refresh_now()/stop() are instant.
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _tick(self) -> float:
        if not self._client.session.authenticated:
            # Nothing to poll until someone logs in.
            return self._config.idle_interval_s

        try:
            self._client.get_auctions(active_only=False)
        except AuctionError as exc:
            return self._handle_failure(exc)

        if self._track_bids:
            for auction_id in self._bid_targets():
                try:
                    self._client.get_bids(auction_id)
                except AuctionError as exc:
                    if exc.kind is ErrorKind.NOT_FOUND:
                        self.unfocus(auction_id)
                    else:
                        return self._handle_failure(exc)

        self._consecutive_failures = 0
        return self._next_interval()

    def _handle_failure(self, exc: AuctionError) -> float:
        self._consecutive_failures += 1
        log.warning("watcher poll failed (%s), failure #%d", exc.kind.value, self._consecutive_failures)

        if exc.kind is ErrorKind.UNAUTHENTICATED:
            # Session is gone and could not be refreshed; idle until a new login.
            self._client.store.set_connection(ConnectionState.DISCONNECTED, exc.user_message())
            return self._config.idle_interval_s

        # Widen the gap while the cluster is unhappy, capped, so a long outage
        # does not spin. resilience.call has already backed off within the attempt.
        penalty = min(2**self._consecutive_failures, 8)
        return min(self._config.idle_interval_s * penalty, 30.0)

    def _bid_targets(self) -> list[str]:
        with self._focus_lock:
            return sorted(self._focus)

    def _next_interval(self) -> float:
        now = time.time()
        cfg = self._config

        if now - self._last_change < cfg.activity_window_s:
            return cfg.fast_interval_s

        for auction in self._client.store.auctions(active_only=True):
            if auction.seconds_remaining(now) <= cfg.urgent_window_s:
                return cfg.fast_interval_s

        return cfg.idle_interval_s

    # -- change tracking ------------------------------------------------------

    def attach_activity_tracking(self) -> None:
        """Subscribe to the store so observed changes speed the poll up."""
        interesting = {
            EventType.BID_PLACED,
            EventType.AUCTION_ADDED,
            EventType.AUCTION_UPDATED,
            EventType.AUCTION_CLOSED,
        }

        def on_event(event) -> None:
            if event.type in interesting:
                self._last_change = time.time()

        self._client.store.subscribe(on_event)
