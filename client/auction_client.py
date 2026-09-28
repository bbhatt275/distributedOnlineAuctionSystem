"""Resilient, state-aware auction client.

This is the layer the CLI, web UI and simulator all talk to. It sits on top of
``client.grpc_client``'s raw stub calls and adds:

* per-operation retry policy (see ``client.resilience``),
* endpoint failover and circuit breaking,
* transparent re-authentication on token rejection,
* ambiguity resolution for the two operations that need it,
* population of the observable ``Store``.

Ambiguity resolution
--------------------
Without an ``idempotency_key`` in the proto, a write that fails *after* the
server applied it is indistinguishable from one that never landed. Two
operations need explicit handling:

``place_bid``   Retrying the same amount is harmless (the server rejects
                ``amount <= current_highest_bid``), but the retry then reports
                BID_TOO_LOW even though the first attempt won. So on that exact
                combination we read the auction back: if we are the highest
                bidder at our amount, the bid succeeded.

``create_auction`` Genuinely unsafe to retry -- a second call mints a second
                auction. Retried only when the failure proves nothing was sent;
                otherwise we list auctions and look for one matching what we
                asked for before deciding.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from client.auth_client import SessionManager
from client.config import CONFIG, ClientConfig
from client.errors import AuctionError, ErrorKind, classify_status
from client.resilience import AT_MOST_ONCE, IDEMPOTENT, NodePool, RetryPolicy, call
from client.state import AuctionView, BidView, ConnectionState, Store

log = logging.getLogger("auction.client")


@dataclass
class BidOutcome:
    accepted: bool
    message: str
    auction: AuctionView | None = None
    # True when the server never confirmed but a read-back proved the bid won.
    # Worth surfacing in the demo: it is the retry logic doing its job.
    recovered: bool = False


@dataclass
class CreateOutcome:
    created: bool
    message: str
    auction_id: str | None = None
    recovered: bool = False


class AuctionClient:
    """High-level auction operations with distributed-failure handling."""

    def __init__(
        self,
        store: Store | None = None,
        pool: NodePool | None = None,
        session: SessionManager | None = None,
        config: ClientConfig | None = None,
    ) -> None:
        self.config = config or CONFIG
        self.store = store or Store()
        self.pool = pool or NodePool(self.config)
        self.session = session or SessionManager(self.pool, self.store)

    # -- plumbing -------------------------------------------------------------

    def _note_connection(self, err: AuctionError | None = None) -> None:
        if err is None:
            self.store.set_connection(ConnectionState.CONNECTED)
        elif err.kind is ErrorKind.PARTITIONED:
            self.store.set_connection(ConnectionState.PARTITIONED, err.user_message())
        elif err.kind.retryable:
            self.store.set_connection(ConnectionState.DEGRADED, err.user_message())

    def _authed(self, policy: RetryPolicy, build):
        """Run an authenticated RPC, re-authenticating once if the token died.

        ``build(token)`` returns an ``invoke(stub, endpoint)`` callable.
        """
        generation = self.session.generation
        token = self.session.require_token()

        try:
            result = call(self.pool, policy, build(token), config=self.config)
        except AuctionError as exc:
            if exc.kind is ErrorKind.UNAUTHENTICATED and self.config.auto_reauth:
                if self.session.reauth(generation):
                    log.info("token refreshed; replaying %s", policy.name)
                    result = call(
                        self.pool, policy, build(self.session.require_token()), config=self.config
                    )
                    self._note_connection()
                    return result
            self._note_connection(exc)
            raise

        self._note_connection()
        return result

    @staticmethod
    def _check(status, endpoint: str | None = None) -> None:
        """Raise if a StatusResponse reports failure."""
        if not status.success:
            raise classify_status(status.message, endpoint=endpoint)

    # -- auth -----------------------------------------------------------------

    def login(self, username: str, password: str) -> str:
        self.store.set_connection(ConnectionState.CONNECTING)
        try:
            token = self.session.login(username, password)
        except AuctionError as exc:
            self._note_connection(exc)
            raise
        self._note_connection()
        return token

    def logout(self) -> bool:
        return self.session.logout()

    # -- reads (all idempotent) ----------------------------------------------

    def get_auctions(self, active_only: bool = False) -> list[AuctionView]:
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

    def get_auction(self, auction_id: str) -> AuctionView | None:
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

    def get_bids(self, auction_id: str) -> list[BidView]:
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

    # -- writes ---------------------------------------------------------------

    def place_bid(self, auction_id: str, amount: float) -> BidOutcome:
        """Place a bid, resolving the retry ambiguity described in the module docstring."""
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

        # Safe to retry: the server's own "must exceed current highest" rule
        # makes a duplicate of the same amount a no-op.
        policy = IDEMPOTENT("PlaceBid")

        try:
            reply = self._authed(policy, build)
        except AuctionError as exc:
            # A retry happened AND the server says the bid is too low: quite
            # possibly our own earlier attempt is what made it too low.
            if exc.kind is ErrorKind.BID_TOO_LOW and exc.attempts > 1:
                if recovered := self._bid_actually_won(auction_id, amount):
                    return recovered
            if exc.kind in (ErrorKind.BID_TOO_LOW, ErrorKind.AUCTION_NOT_ACTIVE, ErrorKind.NOT_FOUND):
                return BidOutcome(accepted=False, message=exc.message, auction=self.store.auction(auction_id))
            raise

        auction = self.get_auction(auction_id)
        return BidOutcome(accepted=True, message=reply.message, auction=auction)

    def _bid_actually_won(self, auction_id: str, amount: float) -> BidOutcome | None:
        """Read back to see whether an ambiguous bid of ours actually landed."""
        try:
            auction = self.get_auction(auction_id)
        except AuctionError:
            return None
        if auction is None:
            return None

        me = self.session.username
        # Float equality is acceptable here only because the value round-trips
        # unchanged through the proto's `double` -- it is the same literal we
        # sent, not the result of arithmetic. (docs/proto-gaps.md argues for
        # integer minor units to remove this class of comparison entirely.)
        if auction.highest_bidder == me and auction.current_highest_bid == amount:
            log.info("bid on %s was applied despite an ambiguous reply", auction_id)
            return BidOutcome(
                accepted=True,
                message="Bid confirmed by read-back after an ambiguous reply",
                auction=auction,
                recovered=True,
            )
        return None

    def create_auction(
        self,
        item_name: str,
        description: str,
        starting_price: float,
        duration_seconds: int,
    ) -> CreateOutcome:
        """Create an auction. Not idempotent server-side, so retried carefully."""
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
                return CreateOutcome(created=False, message=exc.message)
            # Ambiguous: the call may or may not have been applied. Look for it
            # rather than retrying and risking a duplicate auction.
            if found := self._find_created(item_name, starting_price, requested_at):
                return CreateOutcome(
                    created=True,
                    message="Auction confirmed by read-back after an ambiguous reply",
                    auction_id=found,
                    recovered=True,
                )
            raise

        self.get_auction(reply.auction_id)
        return CreateOutcome(created=True, message=reply.message, auction_id=reply.auction_id)

    def _find_created(self, item_name: str, starting_price: float, since: int) -> str | None:
        """Best-effort search for an auction our ambiguous create may have made.

        Matches on (item_name, starting_price, seller-is-us, created at or
        after we asked). Imperfect by construction -- the proto exposes no
        seller field on Auction, so two users creating identical items in the
        same second are indistinguishable. An idempotency_key would make this
        exact; see docs/proto-gaps.md.
        """
        try:
            auctions = self.get_auctions(active_only=False)
        except AuctionError:
            return None

        matches = [
            a
            for a in auctions
            if a.item_name == item_name
            and a.starting_price == starting_price
            and a.start_time >= since - 2
        ]
        if len(matches) == 1:
            return matches[0].auction_id
        if len(matches) > 1:
            log.warning(
                "ambiguous create read-back: %d auctions match %r; not guessing",
                len(matches),
                item_name,
            )
        return None

    def close_auction(self, auction_id: str) -> bool:
        """Close an auction. Idempotent server-side, so freely retryable."""
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

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self.pool.close()
