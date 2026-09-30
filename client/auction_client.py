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


# These entries correspond to the four task types the LLM service defines.
ASSISTANT_TASKS = (
    ("AUCTION_FAQ", "Ask a question", "Rules, bidding, item details"),
    ("ITEM_DESCRIPTION", "Write a description", "Generate a listing description"),
    ("AUCTION_SUMMARY", "Summarise auction", "Price, activity, time left"),
    ("RESULT_SUMMARY", "Summarise result", "Winner, final price, bid count"),
)

ASSISTANT_TASK_IDS = {t[0] for t in ASSISTANT_TASKS}


@dataclass
class AssistantReply:
    answer: str
    task: str
    connected: bool = True
    error: str | None = None


def _tidy_llm_error(message):
    """The server wraps gRPC failures as "LLM server error: <repr>", which is
    unreadable in a chat bubble. Collapse the common ones."""
    text = (message or "").strip()
    low = text.lower()

    if "unavailable" in low or "failed to connect" in low or "connection refused" in low:
        return "The LLM node is not reachable. Is it running on port 50052?"
    if "deadline" in low or "timeout" in low:
        return "The LLM took too long to answer."
    if text.startswith("LLM server error:"):
        return "The LLM node returned an error."
    return text


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
            # The call was retried and the server reports the bid as too low,
            # which may be because an earlier attempt of ours succeeded and
            # raised the highest bid.
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

        # Comparing these values directly is safe, because this is the same
        # number that was sent and returned rather than a computed one.
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

            # The outcome is unclear, so the auction is searched for rather
            # than created again, which would risk producing two of them.
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

        This is approximate. An auction carries no seller field, so two users
        creating the same item within the same second cannot be told apart. An
        idempotency key on the request would make the search exact.
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

    def assistant_route_available(self):
        """Whether the app server exposes AskLLM."""
        from generated import auction_pb2
        service = auction_pb2.DESCRIPTOR.services_by_name.get("AuctionService")
        return bool(service and service.methods_by_name.get("AskLLM"))

    def ask_assistant(self, task, query, auction_id=None):
        """Ask the LLM through the application server.

        The application server performs the call to the LLM server. It builds
        the auction context, so only the task type and the question are sent
        from here.
        """
        if task not in ASSISTANT_TASK_IDS:
            raise ValueError(f"unknown task {task!r}")

        query = (query or "").strip()
        if not query:
            raise ValueError("query is empty")

        if not self.assistant_route_available():
            return AssistantReply(
                answer="The application server doesn't expose AskLLM yet.",
                task=task,
                connected=False,
            )

        from generated import auction_pb2, llm_pb2

        task_value = llm_pb2.LLMTaskType.Value(task)

        def build(token):
            def invoke(stub, endpoint):
                reply = stub.AskLLM(
                    auction_pb2.AskLLMRequest(
                        token=token,
                        query=query,
                        task_type=task_value,
                        auction_id=auction_id or "",
                    ),
                    timeout=self.config.llm_timeout_s,
                )
                if not reply.success:
                    err = classify_status(reply.message, endpoint=endpoint)
                    # Only an authentication failure is raised to the retry
                    # loop, so that an unavailable LLM server is reported once
                    # rather than attempted repeatedly.
                    if err.kind is ErrorKind.UNAUTHENTICATED:
                        raise err
                return reply
            return invoke

        try:
            # A timeout may mean the model is still generating a reply, so
            # sending the request again would queue a second costly run behind
            # the first. The call is therefore made at most once.
            reply = self._authed(AT_MOST_ONCE("AskLLM"), build)
        except AuctionError as exc:
            return AssistantReply("", task, error=exc.user_message())

        if not reply.success:
            return AssistantReply("", task, error=_tidy_llm_error(reply.message))

        return AssistantReply(reply.answer, task)

    def close(self):
        self.pool.close()
