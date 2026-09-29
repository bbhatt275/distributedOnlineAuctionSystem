import time
import uuid

from generated import auction_pb2


class AuctionManager:

    def __init__(self, state_store):
        self.state_store = state_store

    def create_auction(
        self,
        item_name,
        description,
        starting_price,
        duration_seconds,
        creator
    ):
        auction_id = str(uuid.uuid4())

        start_time = int(time.time())
        end_time = start_time + duration_seconds

        auction = auction_pb2.Auction(
            auction_id=auction_id,
            item_name=item_name,
            description=description,
            starting_price=starting_price,
            current_highest_bid=starting_price,
            highest_bidder="",
            start_time=start_time,
            end_time=end_time,
            active=True,
            winner=""
        )

        with self.state_store.lock:
            self.state_store.auctions[auction_id] = auction
            self.state_store.bids[auction_id] = []

        return auction

    def get_auction(self, auction_id):
        with self.state_store.lock:
            return self.state_store.auctions.get(auction_id)

    def get_auctions(self, active_only=False):
        with self.state_store.lock:
            auctions = list(self.state_store.auctions.values())

        if active_only:
            auctions = [
                auction for auction in auctions
                if auction.active
            ]

        return auctions

    def place_bid(self, auction_id, bidder, amount):
        with self.state_store.lock:

            auction = self.state_store.auctions.get(auction_id)

            if auction is None:
                return False, "Auction not found", None

            # Check whether auction has expired
            if time.time() >= auction.end_time:
                auction.active = False
                auction.winner = auction.highest_bidder

                return False, "Auction has ended", None

            if not auction.active:
                return False, "Auction is not active", None

            # Bid must be greater than current highest bid
            if amount <= auction.current_highest_bid:
                return (
                    False,
                    "Bid must be higher than the current highest bid",
                    None
                )

            bid = auction_pb2.Bid(
                bid_id=str(uuid.uuid4()),
                auction_id=auction_id,
                bidder=bidder,
                amount=amount,
                timestamp=int(time.time())
            )

            # Update auction state
            auction.current_highest_bid = amount
            auction.highest_bidder = bidder

            # Store bid
            self.state_store.bids[auction_id].append(bid)

            return True, "Bid placed successfully", bid
    def get_bids(self, auction_id):
        with self.state_store.lock:
            return self.state_store.bids.get(auction_id)
