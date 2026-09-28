"""Auction operations with retry and state tracking.

This is what the CLI, web app and simulator all use. Sits on top of the
generated stubs and adds retry policy, failover, re-authentication and Store
updates.

Two operations need extra care because the proto has no idempotency key:

  place_bid       retrying the same amount is harmless, but the retry then
                  reports "bid too low" even when our first attempt won, so we
                  read the auction back to find out which happened
  create_auction  genuinely unsafe to resend, so we only retry when the
                  failure proves nothing was sent, and otherwise go looking
                  for the auction we may have created
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from client.auth_client import SessionManager
from client.config import CONFIG
from client.errors import AuctionError, ErrorKind, classify_status
from client.resilience import AT_MOST_ONCE, IDEMPOTENT, NodePool, call
from client.state import AuctionView, BidView, ConnectionState, Store

log = logging.getLogger("auction.client")


@dataclass
class BidOutcome:
    accepted: bool
    message: str
    auction: AuctionView | None = None
    recovered: bool = False


@dataclass
class CreateOutcome:
    created: bool
    message: str
    auction_id: str | None = None
    recovered: bool = False


class AuctionClient:

    def __init__(self, store=None, pool=None, session=None, config=None):
        self.config = config or CONFIG
        self.store = store or Store()
        self.pool = pool or NodePool(self.config)
        self.session = session or SessionManager(self.pool, self.store)

    def _note_connection(self, err=None):
        if err is None:
            self.store.set_connection(ConnectionState.CONNECTED)
        elif err.kind is ErrorKind.PARTITIONED:
            self.store.set_connection(ConnectionState.PARTITIONED, err.user_message())
        elif err.kind.retryable:
            self.store.set_connection(ConnectionState.DEGRADED, err.user_message())

    def _authed(self, policy, build):
        """Run an authenticated RPC, re-authenticating once if the token died.

        build(token) returns the invoke(stub, endpoint) callable.
        """
        generation = self.session.generation
        token = self.session.require_token()

        try:
            result = call(self.pool, policy, build(token), config=self.config)
        except AuctionError as exc:
            if exc.kind is ErrorKind.UNAUTHENTICATED and self.config.auto_reauth:
                if self.session.reauth(generation):
                    log.info("token refreshed, replaying %s", policy.name)
                    result = call(self.pool, policy,
                                  build(self.session.require_token()), config=self.config)
                    self._note_connection()
                    return result
            self._note_connection(exc)
            raise

        self._note_connection()
        return result

    @staticmethod
    def _check(status, endpoint=None):
        if not status.success:
            raise classify_status(status.message, endpoint=endpoint)

    def login(self, username, password):
        self.store.set_connection(ConnectionState.CONNECTING)
        try:
            token = self.session.login(username, password)
        except AuctionError as exc:
            self._note_connection(exc)
            raise
        self._note_connection()
        return token

    def logout(self):
        return self.session.logout()

    def get_auctions(self, active_only=False):
        from generated import auction_pb2

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Get(
                    auction_pb2.GetRequest(
                        token=token,
                        get_auctions=auction_pb2.GetAuctionsRequest(active_only=active_only),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                self._check(reply.status, endpoint)
                return reply
            return invoke

        reply = self._authed(IDEMPOTENT("GetAuctions"), build)
        self.store.apply_auctions(reply.auctions)
        return [AuctionView.from_proto(a) for a in reply.auctions]

    def get_auction(self, auction_id):
        from generated import auction_pb2

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Get(
                    auction_pb2.GetRequest(
                        token=token,
                        get_auction=auction_pb2.GetAuctionRequest(auction_id=auction_id),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                self._check(reply.status, endpoint)
                return reply
            return invoke

        try:
            reply = self._authed(IDEMPOTENT("GetAuction"), build)
        except AuctionError as exc:
            if exc.kind is ErrorKind.NOT_FOUND:
                return None
            raise

        self.store.apply_auctions(reply.auctions)
        return AuctionView.from_proto(reply.auctions[0]) if reply.auctions else None

    def get_bids(self, auction_id):
        from generated import auction_pb2

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Get(
                    auction_pb2.GetRequest(
                        token=token,
                        get_bids=auction_pb2.GetBidsRequest(auction_id=auction_id),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                self._check(reply.status, endpoint)
                return reply
            return invoke

        reply = self._authed(IDEMPOTENT("GetBids"), build)
        self.store.apply_bids(auction_id, reply.bids)
        return [BidView.from_proto(b) for b in reply.bids]

    def place_bid(self, auction_id, amount):
        from generated import auction_pb2

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Post(
                    auction_pb2.PostRequest(
                        token=token,
                        place_bid=auction_pb2.PlaceBidRequest(
                            auction_id=auction_id, amount=amount
                        ),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                if not reply.success:
                    raise classify_status(reply.message, endpoint=endpoint)
                return reply
            return invoke

        try:
            reply = self._authed(IDEMPOTENT("PlaceBid"), build)
        except AuctionError as exc:
            # We retried and the server says too low -- possibly because our
            # own earlier attempt is what raised the bar.
            if exc.kind is ErrorKind.BID_TOO_LOW and exc.attempts > 1:
                recovered = self._bid_actually_won(auction_id, amount)
                if recovered:
                    return recovered

            if exc.kind in (ErrorKind.BID_TOO_LOW, ErrorKind.AUCTION_NOT_ACTIVE,
                            ErrorKind.NOT_FOUND):
                return BidOutcome(False, exc.message, self.store.auction(auction_id))
            raise

        auction = self.get_auction(auction_id)
        return BidOutcome(True, reply.message, auction)

    def _bid_actually_won(self, auction_id, amount):
        try:
            auction = self.get_auction(auction_id)
        except AuctionError:
            return None

        if auction is None:
            return None

        # Float equality is fine here: this is the same literal we sent,
        # round-tripped through the proto, not the result of arithmetic.
        if auction.highest_bidder == self.session.username \
                and auction.current_highest_bid == amount:
            log.info("bid on %s landed despite an ambiguous reply", auction_id)
            return BidOutcome(
                True,
                "Bid confirmed by read-back after an ambiguous reply",
                auction,
                recovered=True,
            )

        return None

    def create_auction(self, item_name, description, starting_price, duration_seconds):
        from generated import auction_pb2

        requested_at = int(time.time())

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Post(
                    auction_pb2.PostRequest(
                        token=token,
                        create_auction=auction_pb2.CreateAuctionRequest(
                            item_name=item_name,
                            description=description,
                            starting_price=starting_price,
                            duration_seconds=duration_seconds,
                        ),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                if not reply.success:
                    raise classify_status(reply.message, endpoint=endpoint)
                return reply
            return invoke

        try:
            reply = self._authed(AT_MOST_ONCE("CreateAuction"), build)
        except AuctionError as exc:
            if exc.kind is ErrorKind.INVALID_ARGUMENT:
                return CreateOutcome(False, exc.message)

            # Ambiguous. Go looking rather than resending and risking two.
            found = self._find_created(item_name, starting_price, requested_at)
            if found:
                return CreateOutcome(
                    True,
                    "Auction confirmed by read-back after an ambiguous reply",
                    found,
                    recovered=True,
                )
            raise

        self.get_auction(reply.auction_id)
        return CreateOutcome(True, reply.message, reply.auction_id)

    def _find_created(self, item_name, starting_price, since):
        """Look for an auction our ambiguous create may have made.

        Imperfect -- Auction has no seller field, so two users creating the
        same item in the same second are indistinguishable. An idempotency key
        would make this exact.
        """
        try:
            auctions = self.get_auctions(active_only=False)
        except AuctionError:
            return None

        matches = [
            a for a in auctions
            if a.item_name == item_name
            and a.starting_price == starting_price
            and a.start_time >= since - 2
        ]

        if len(matches) == 1:
            return matches[0].auction_id
        if len(matches) > 1:
            log.warning("ambiguous read-back: %d auctions match %r", len(matches), item_name)
        return None

    def close_auction(self, auction_id):
        from generated import auction_pb2

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.Post(
                    auction_pb2.PostRequest(
                        token=token,
                        close_auction=auction_pb2.CloseAuctionRequest(auction_id=auction_id),
                    ),
                    timeout=self.config.rpc_timeout_s,
                )
                if not reply.success:
                    raise classify_status(reply.message, endpoint=endpoint)
                return reply
            return invoke

        self._authed(IDEMPOTENT("CloseAuction"), build)
        self.get_auction(auction_id)
        return True

    def close(self):
        self.pool.close()
