import json
import threading
import time
import uuid

from application.llm_client import LLMClient
from generated import llm_pb2
from generated import auction_pb2
from generated import auction_pb2_grpc
from application.auth_service import AuthService
from application.auction_manager import AuctionManager
from application.state_store import StateStore
from application.escrow import MockEscrow


class AuctionService(auction_pb2_grpc.AuctionServiceServicer):
    def __init__(self, raft_node, election_manager, state_store=None):
        self.raft_node = raft_node
        self.election_manager = election_manager
        self.auth_service = AuthService()
        self.state_store = state_store or StateStore()
        self.auction_manager = AuctionManager(self.state_store)
        self.llm_client = LLMClient()
        self.escrow = MockEscrow()

        # Serialize leader-side validation + proposal so two concurrent RPCs
        # cannot both validate against the same old auction state.
        self.write_lock = threading.RLock()
        self.auction_manager.start_watcher(
            self._close_expired_auction,
            is_leader=lambda: self.raft_node.role == "LEADER",
        )

    def apply_command(self, command_bytes):
        """Raft callback: apply one committed command to this node's state."""
        command = json.loads(command_bytes.decode("utf-8"))
        operation = command.get("operation")

        if operation == "CREATE_AUCTION":
            self.auction_manager.apply_create(command["auction"])
        elif operation == "PLACE_BID":
            self.auction_manager.apply_bid(command["bid"])
        elif operation == "CLOSE_AUCTION":
            self.auction_manager.apply_close(command["auction_id"])
        else:
            raise ValueError(f"Unknown Raft command: {operation}")

    def _propose(self, command):
        payload = json.dumps(
            command, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return self.election_manager.propose_command(payload)

    def _not_leader_response(self):
        leader = self.raft_node.leader_id
        message = "Not leader"
        if leader:
            message += f". Current leader: {leader}"
        return auction_pb2.StatusResponse(
            success=False,
            message=message,
        )

    def _close_expired_auction(self, auction_id):
        with self.write_lock:
            if self.raft_node.role != "LEADER":
                raise RuntimeError("This node is no longer the leader")

            auction = self.auction_manager.get_auction(auction_id)
            if auction is None or not auction.active:
                return
            if time.time() < auction.end_time:
                return

            self._propose({
                "operation": "CLOSE_AUCTION",
                "auction_id": auction_id,
            })

            if auction.winner:
                self.escrow.release_funds(
                    auction_id=auction_id,
                    winner=auction.winner,
                )

    def AskLLM(self, request, context):
        user = self.auth_service.validate_token(request.token)
        if user is None:
            return auction_pb2.AskLLMResponse(
                success=False, message="Not authenticated", answer=""
            )

        if not request.query.strip():
            return auction_pb2.AskLLMResponse(
                success=False, message="Query cannot be empty", answer=""
            )

        auction_context = None
        additional_context = ""

        if request.auction_id:
            auction = self.auction_manager.get_auction(request.auction_id)
            if auction is None:
                return auction_pb2.AskLLMResponse(
                    success=False, message="Auction not found", answer=""
                )

            bids = self.auction_manager.get_bids(request.auction_id)
            auction_context = llm_pb2.AuctionContext(
                auction_id=auction.auction_id,
                item_name=auction.item_name,
                item_description=auction.description,
                starting_price=auction.starting_price,
                current_highest_bid=auction.current_highest_bid,
                currency="INR",
                active=auction.active,
                start_time=auction.start_time,
                end_time=auction.end_time,
                bid_count=len(bids),
                highest_bidder=auction.highest_bidder,
                winner=auction.winner,
            )
            additional_context = f"Auction creator: {auction.creator}\n"

            for bid in bids:
                auction_context.bids.add(
                    bid_id=bid.bid_id,
                    bidder=bid.bidder,
                    amount=bid.amount,
                    timestamp=bid.timestamp,
                )
        else:
            active_auctions = self.auction_manager.get_auctions(active_only=True)
            context_parts = []

            for auction in active_auctions:
                bids = self.auction_manager.get_bids(auction.auction_id)
                auction_text = f"""
Auction ID: {auction.auction_id}
Item: {auction.item_name}
Description: {auction.description}
Starting Price: ₹{auction.starting_price}
Current Highest Bid: ₹{auction.current_highest_bid}
Highest Bidder: {auction.highest_bidder}
Creator: {auction.creator}
Active: {auction.active}
Start Time: {auction.start_time}
End Time: {auction.end_time}
Number of Bids: {len(bids)}
"""
                if bids:
                    auction_text += "\nBid History:\n"
                    for bid in bids:
                        auction_text += (
                            f"- Bidder: {bid.bidder}, Amount: ₹{bid.amount}, "
                            f"Timestamp: {bid.timestamp}\n"
                        )
                context_parts.append(auction_text)

            additional_context = (
                "The following are the currently ACTIVE auctions. "
                "Use this information when answering the user's query.\n\n"
                + "\n--------------------\n".join(context_parts)
                if context_parts
                else "There are currently no active auctions."
            )

        requester_context = llm_pb2.RequesterContext(user_id=user)

        try:
            response = self.llm_client.get_answer(
                request_id=str(uuid.uuid4()),
                task_type=request.task_type,
                query=request.query,
                auction_context=auction_context,
                requester_context=requester_context,
                additional_context=additional_context,
            )
        except Exception as exc:
            return auction_pb2.AskLLMResponse(
                success=False,
                message=f"LLM server error: {exc}",
                answer="",
            )

        return auction_pb2.AskLLMResponse(
            success=response.success,
            message=response.message,
            answer=response.answer,
        )

    def Login(self, request, context):
        # Tokens are node-local in this project, so issue them only on the leader.
        if self.raft_node.role != "LEADER":
            return auction_pb2.LoginResponse(
                status=self._not_leader_response(),
                token="",
            )

        token = self.auth_service.login(request.username, request.password)
        if token is None:
            return auction_pb2.LoginResponse(
                status=auction_pb2.StatusResponse(
                    success=False, message="Invalid credentials"
                ),
                token="",
            )
        return auction_pb2.LoginResponse(
            status=auction_pb2.StatusResponse(
                success=True, message="Login successful"
            ),
            token=token,
        )

    def Logout(self, request, context):
        return auction_pb2.StatusResponse(
            success=self.auth_service.logout(request.token),
            message="Logout successful",
        )

    def Post(self, request, context):
        # Check leadership before token validation. This lets the client learn
        # the new leader after failover; it can then ask the user to log in again.
        if self.raft_node.role != "LEADER":
            return self._not_leader_response()

        user = self.auth_service.validate_token(request.token)
        if user is None:
            return auction_pb2.StatusResponse(
                success=False, message="Not authenticated"
            )

        with self.write_lock:
            # Recheck leadership after acquiring the write lock.
            if self.raft_node.role != "LEADER":
                return self._not_leader_response()

            if request.HasField("create_auction"):
                req = request.create_auction
                if req.starting_price < 0:
                    return auction_pb2.StatusResponse(
                        success=False, message="Starting price cannot be negative"
                    )
                if req.duration_seconds <= 0:
                    return auction_pb2.StatusResponse(
                        success=False, message="Duration must be positive"
                    )

                auction = self.auction_manager.build_auction(
                    item_name=req.item_name,
                    description=req.description,
                    starting_price=req.starting_price,
                    duration_seconds=req.duration_seconds,
                    creator=user,
                )
                auction_data = {
                    field.name: getattr(auction, field.name)
                    for field in auction.DESCRIPTOR.fields
                }

                try:
                    self._propose({
                        "operation": "CREATE_AUCTION",
                        "auction": auction_data,
                    })
                except (RuntimeError, TimeoutError) as exc:
                    return auction_pb2.StatusResponse(
                        success=False, message=f"Could not commit auction: {exc}"
                    )

                return auction_pb2.StatusResponse(
                    success=True,
                    message=f"Auction created: {auction.auction_id}",
                    auction_id=auction.auction_id,
                )

            if request.HasField("place_bid"):
                req = request.place_bid
                auction = self.auction_manager.get_auction(req.auction_id)

                if auction is None:
                    return auction_pb2.StatusResponse(
                        success=False, message="Auction not found"
                    )
                if time.time() >= auction.end_time or not auction.active:
                    return auction_pb2.StatusResponse(
                        success=False, message="Auction has ended"
                    )
                if req.amount <= auction.current_highest_bid:
                    return auction_pb2.StatusResponse(
                        success=False,
                        message="Bid must be higher than the current highest bid",
                    )

                previous_highest_bidder = auction.highest_bidder
                bid = self.auction_manager.build_bid(
                    req.auction_id, user, req.amount
                )
                bid_data = {
                    field.name: getattr(bid, field.name)
                    for field in bid.DESCRIPTOR.fields
                }

                try:
                    self._propose({
                        "operation": "PLACE_BID",
                        "bid": bid_data,
                    })
                except (RuntimeError, TimeoutError) as exc:
                    return auction_pb2.StatusResponse(
                        success=False, message=f"Could not commit bid: {exc}"
                    )

                # External effects occur only after the bid is committed.
                escrow_success, _ = self.escrow.reserve_funds(
                    bidder=user,
                    auction_id=req.auction_id,
                    amount=req.amount,
                )
                if not escrow_success:
                    # The replicated bid is already committed; do not pretend
                    # that it was rolled back.
                    return auction_pb2.StatusResponse(
                        success=True,
                        message="Bid committed, but mock escrow reservation failed",
                        auction_id=req.auction_id,
                    )

                if previous_highest_bidder:
                    self.escrow.refund_funds(
                        auction_id=req.auction_id,
                        bidder=previous_highest_bidder,
                    )

                return auction_pb2.StatusResponse(
                    success=True,
                    message="Bid placed successfully",
                    auction_id=req.auction_id,
                )

            if request.HasField("close_auction"):
                auction_id = request.close_auction.auction_id
                auction = self.auction_manager.get_auction(auction_id)

                if auction is None:
                    return auction_pb2.StatusResponse(
                        success=False, message="Auction not found"
                    )
                if auction.creator != user:
                    return auction_pb2.StatusResponse(
                        success=False,
                        message="You can only close auctions that you created",
                    )

                winner = auction.highest_bidder
                try:
                    self._propose({
                        "operation": "CLOSE_AUCTION",
                        "auction_id": auction_id,
                    })
                except (RuntimeError, TimeoutError) as exc:
                    return auction_pb2.StatusResponse(
                        success=False, message=f"Could not commit close: {exc}"
                    )

                if winner:
                    self.escrow.release_funds(
                        auction_id=auction_id,
                        winner=winner,
                    )

                return auction_pb2.StatusResponse(
                    success=True,
                    message="Auction closed successfully",
                    auction_id=auction_id,
                )

        return auction_pb2.StatusResponse(
            success=False, message="No valid operation specified"
        )

    def Get(self, request, context):
        user = self.auth_service.validate_token(request.token)
        if user is None:
            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=False, message="Not Authenticated"
                )
            )

        if request.HasField("get_auction"):
            auction = self.auction_manager.get_auction(
                request.get_auction.auction_id
            )
            if auction is None:
                return auction_pb2.GetResponse(
                    status=auction_pb2.StatusResponse(
                        success=False, message="Auction not found"
                    )
                )
            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=True, message="Auction found"
                ),
                auctions=[auction],
            )

        if request.HasField("get_auctions"):
            auctions = self.auction_manager.get_auctions(
                active_only=request.get_auctions.active_only
            )
            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=True, message="Auctions retrieved"
                ),
                auctions=auctions,
            )

        if request.HasField("get_bids"):
            bids = self.auction_manager.get_bids(
                request.get_bids.auction_id
            )
            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=True, message="Bids retrieved"
                ),
                bids=bids,
            )

        return auction_pb2.GetResponse(
            status=auction_pb2.StatusResponse(
                success=False, message="No valid query specified"
            )
        )
