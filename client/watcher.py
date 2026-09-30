"""Background polling that keeps the Store fresh.

auction.proto has no streaming RPC, so live updates are faked by polling and
letting the Store diff each snapshot into events. Subscribers can't tell the
difference, which means only this file changes if a real stream shows up later.

The interval adapts so the demo stays responsive without hammering the
server's 10-worker pool.
"""

from __future__ import annotations

import logging
import threading
import time

from client.config import CONFIG
from client.errors import ErrorKind, AuctionError
from client.state import ConnectionState, EventType

log = logging.getLogger("auction.client.watcher")


class AuctionWatcher:

    def __init__(self, client, config=None, track_bids=True):
        self._client = client
        self._config = config or CONFIG.watch
        self._track_bids = track_bids
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_change = 0.0
        self._consecutive_failures = 0
        # Polling bids for every auction would be one RPC each per tick, so
        # detail is opt-in -- the UI registers whatever the user is looking at.
        self._focus = set()
        self._focus_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auction-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()

    def focus(self, auction_id):
        with self._focus_lock:
            self._focus.add(auction_id)
        self.refresh_now()

    def unfocus(self, auction_id):
        with self._focus_lock:
            self._focus.discard(auction_id)

    def refresh_now(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            interval = self._tick()
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _tick(self):
        if not self._client.session.authenticated:
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

    def _handle_failure(self, exc):
        self._consecutive_failures += 1
        log.warning("poll failed (%s), failure #%d", exc.kind.value, self._consecutive_failures)

        if exc.kind is ErrorKind.UNAUTHENTICATED:
            self._client.store.set_connection(ConnectionState.DISCONNECTED, exc.user_message())
            return self._config.idle_interval_s

        # Back off while the cluster is unhappy. resilience.call has already
        # backed off within the attempt itself.
        penalty = min(2 ** self._consecutive_failures, 8)
        return min(self._config.idle_interval_s * penalty, 30.0)

    def _bid_targets(self):
        with self._focus_lock:
            return sorted(self._focus)

    def _next_interval(self):
        now = time.time()
        cfg = self._config

        if now - self._last_change < cfg.activity_window_s:
            return cfg.fast_interval_s

        for auction in self._client.store.auctions(active_only=True):
            if auction.seconds_remaining(now) <= cfg.urgent_window_s:
                return cfg.fast_interval_s

        return cfg.idle_interval_s

    def attach_activity_tracking(self):
        """Watch the store so observed changes speed the poll up."""
        interesting = {
            EventType.BID_PLACED,
            EventType.AUCTION_ADDED,
            EventType.AUCTION_UPDATED,
            EventType.AUCTION_CLOSED,
        }

        def on_event(event):
            if event.type in interesting:
                self._last_change = time.time()

        self._client.store.subscribe(on_event)
