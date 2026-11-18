import threading
import time
import uuid

from generated import auction_pb2


class AuctionManager:
    def __init__(self, state_store):
        self.state_store = state_store
        self._expiry_handler = None
        self._expiry_pending = set()
        self._watcher_started = False

    def start_watcher(self, expiry_handler, is_leader):
        """Start the expiry watcher once the Raft-backed handler is available."""
        self._expiry_handler = expiry_handler
        self._is_leader = is_leader
        if self._watcher_started:
            return
        self._watcher_started = True
        threading.Thread(target=self.track_auction, daemon=True).start()

    def track_auction(self):
        while True:
            time.sleep(1)

            if self._expiry_handler is None or not self._is_leader():
                continue

            # Only the leader proposes expiry commands. Followers never mutate
            # their local state independently.
            for auction in self.get_auctions(active_only=True):
                if time.time() < auction.end_time:
                    continue

                auction_id = auction.auction_id
                with self.state_store.lock:
                    if auction_id in self._expiry_pending:
                        continue
                    self._expiry_pending.add(auction_id)

                try:
                    self._expiry_handler(auction_id)
                except Exception as exc:
                    with self.state_store.lock:
                        self._expiry_pending.discard(auction_id)
                    print(f"[AuctionManager] Expiry proposal failed: {exc}")

    def build_auction(
        self,
        item_name,
        description,
        starting_price,
        duration_seconds,
        creator,
    ):
        start_time = int(time.time())
        return auction_pb2.Auction(
            auction_id=str(uuid.uuid4()),
            item_name=item_name,
            description=description,
            starting_price=starting_price,
            current_highest_bid=starting_price,
            highest_bidder="",
            start_time=start_time,
            end_time=start_time + duration_seconds,
            active=True,
            winner="",
            creator=creator,
        )

    def build_bid(self, auction_id, bidder, amount):
        return auction_pb2.Bid(
            bid_id=str(uuid.uuid4()),
            auction_id=auction_id,
            bidder=bidder,
            amount=amount,
            timestamp=int(time.time()),
        )

    # The following methods are deterministic state-machine operations.
    # They are called by apply_command after Raft commits the command.

    def apply_create(self, data):
        auction = auction_pb2.Auction(**data)
        with self.state_store.lock:
            if auction.auction_id in self.state_store.auctions:
                return
            self.state_store.auctions[auction.auction_id] = auction
            self.state_store.bids[auction.auction_id] = []

    def apply_bid(self, data):
        bid = auction_pb2.Bid(**data)
        with self.state_store.lock:
            auction = self.state_store.auctions.get(bid.auction_id)
            if auction is None or not auction.active:
                print(f"[AuctionManager] Ignored invalid committed bid {bid.bid_id}")
                return

            # A repeated command must not append the same bid twice.
            existing = self.state_store.bids.setdefault(bid.auction_id, [])
            if any(item.bid_id == bid.bid_id for item in existing):
                return

            # Commands are applied in log order. This guard protects the state
            # machine from malformed or stale commands.
            if bid.amount <= auction.current_highest_bid:
                print(f"[AuctionManager] Ignored non-increasing bid {bid.bid_id}")
                return

            auction.current_highest_bid = bid.amount
            auction.highest_bidder = bid.bidder
            existing.append(bid)

    def apply_close(self, auction_id):
        with self.state_store.lock:
            auction = self.state_store.auctions.get(auction_id)
            if auction is None:
                return
            auction.active = False
            auction.winner = auction.highest_bidder
            self._expiry_pending.discard(auction_id)

    def get_auction(self, auction_id):
        with self.state_store.lock:
            return self.state_store.auctions.get(auction_id)

    def get_auctions(self, active_only=False):
        with self.state_store.lock:
            auctions = list(self.state_store.auctions.values())
        if active_only:
            auctions = [auction for auction in auctions if auction.active]
        return auctions

    def get_bids(self, auction_id):
        with self.state_store.lock:
            return list(self.state_store.bids.get(auction_id, []))

    # Compatibility helpers for isolated unit tests. Do not call these from
    # distributed RPC write paths; those must go through Raft proposals.
    def create_auction(
        self, item_name, description, starting_price,
        duration_seconds, creator
    ):
        auction = self.build_auction(
            item_name, description, starting_price,
            duration_seconds, creator
        )
        self.apply_create({
            field: getattr(auction, field)
            for field in auction.DESCRIPTOR.fields_by_name
        })
        return auction

    def place_bid(self, auction_id, bidder, amount):
        auction = self.get_auction(auction_id)
        if auction is None:
            return False, "Auction not found", None
        if time.time() >= auction.end_time:
            return False, "Auction has ended", None
        if not auction.active:
            return False, "Auction is not active", None
        if amount <= auction.current_highest_bid:
            return False, "Bid must be higher than the current highest bid", None

        bid = self.build_bid(auction_id, bidder, amount)
        self.apply_bid({
            field: getattr(bid, field)
            for field in bid.DESCRIPTOR.fields_by_name
        })
        return True, "Bid placed successfully", bid

    def close_auction(self, auction_id):
        auction = self.get_auction(auction_id)
        if auction is None:
            return False, "Auction not found"
        self.apply_close(auction_id)
        return True, "Auction closed successfully"
