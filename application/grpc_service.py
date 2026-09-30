from application.llm_client import LLMClient
from generated import llm_pb2
import uuid
from generated import auction_pb2
from generated import auction_pb2_grpc
from application.auth_service import AuthService
from application.auction_manager import AuctionManager
from application.state_store import StateStore
from application.escrow import MockEscrow

class AuctionService(auction_pb2_grpc.AuctionServiceServicer):
    def __init__(self):
        self.auth_service = AuthService()
        self.state_store = StateStore()
        self.auction_manager = AuctionManager(self.state_store)
        self.llm_client = LLMClient()
        self.escrow = MockEscrow()

    def AskLLM(self, request, context):

        # -----------------------------------------
        # 1. Authenticate user
        # -----------------------------------------

        user = self.auth_service.validate_token(request.token)

        if user is None:
            return auction_pb2.AskLLMResponse(
                success=False,
                message="Not authenticated",
                answer=""
            )

        # -----------------------------------------
        # 2. Validate query
        # -----------------------------------------

        if not request.query.strip():
            return auction_pb2.AskLLMResponse(
                success=False,
                message="Query cannot be empty",
                answer=""
            )

        # -----------------------------------------
        # 3. Build auction context
        # -----------------------------------------

        auction_context = None
        additional_context = ""

        # -----------------------------------------
        # Case 1: Specific auction page
        # -----------------------------------------

        if request.auction_id:

            auction = self.auction_manager.get_auction(
                request.auction_id
            )

            if auction is None:
                return auction_pb2.AskLLMResponse(
                    success=False,
                    message="Auction not found",
                    answer=""
                )

            bids = self.auction_manager.get_bids(
                request.auction_id
            )

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
                bid_count=len(bids) if bids else 0,
                highest_bidder=auction.highest_bidder,
                winner=auction.winner
            )
            additional_context = (
                f"Auction creator: {auction.creator}\n"
            )

            # Add bid history
            if bids:
                for bid in bids:
                    auction_context.bids.add(
                        bid_id=bid.bid_id,
                        bidder=bid.bidder,
                        amount=bid.amount,
                        timestamp=bid.timestamp
                    )

        # -----------------------------------------
        # Case 2: Dashboard / Global chatbot
        # -----------------------------------------

        else:

            active_auctions = self.auction_manager.get_auctions(
                active_only=True
            )

            context_parts = []

            for auction in active_auctions:

                bids = self.auction_manager.get_bids(
                    auction.auction_id
                )

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
    Number of Bids: {len(bids) if bids else 0}
    """

                # Add bid history
                if bids:
                    auction_text += "\nBid History:\n"

                    for bid in bids:
                        auction_text += (
                            f"- Bidder: {bid.bidder}, "
                            f"Amount: ₹{bid.amount}, "
                            f"Timestamp: {bid.timestamp}\n"
                        )

                context_parts.append(auction_text)

            if context_parts:
                additional_context = (
                        "The following are the currently ACTIVE auctions. "
                        "Use this information when answering the user's query.\n\n"
                        + "\n--------------------\n".join(context_parts)
                )
            else:
                additional_context = (
                    "There are currently no active auctions."
                )

        # -----------------------------------------
        # 4. Build requester context
        # -----------------------------------------

        requester_context = llm_pb2.RequesterContext(
            user_id=user
        )

        # -----------------------------------------
        # 5. Call LLM server
        # -----------------------------------------

        try:

            response = self.llm_client.get_answer(
                request_id=str(uuid.uuid4()),
                task_type=request.task_type,
                query=request.query,
                auction_context=auction_context,
                requester_context=requester_context,
                additional_context=additional_context
            )

        except Exception as e:

            return auction_pb2.AskLLMResponse(
                success=False,
                message=f"LLM server error: {str(e)}",
                answer=""
            )

        # -----------------------------------------
        # 6. Return LLM response to client
        # -----------------------------------------

        return auction_pb2.AskLLMResponse(
            success=response.success,
            message=response.message,
            answer=response.answer
        )

    def Login(self, request, context):
        token = self.auth_service.login(request.username, request.password)
        if token is None:
            return auction_pb2.LoginResponse(
                status = auction_pb2.StatusResponse(
                    success = False,
                    message = "Invalid credentials"
                ),
                token = ""
            )
        return auction_pb2.LoginResponse(
            status = auction_pb2.StatusResponse(
                success = True,
                message = "Login successful"
            ),
            token = token
        )

    def Logout(self, request, context):
        return auction_pb2.StatusResponse(
            success=self.auth_service.logout(request.token),
            message="Logout successful"
        )

    def Post(self, request, context):
        user = self.auth_service.validate_token(request.token)
        if user is None:
            return auction_pb2.StatusResponse(
                success=False,
                message = "Not authenticated"
            )
        if request.HasField("create_auction"):
            auction_request = request.create_auction

            if auction_request.starting_price < 0:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="Starting price cannot be negative"
                )

            if auction_request.duration_seconds <= 0:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="Duration must be positive"
                )

            auction = self.auction_manager.create_auction(
                item_name=auction_request.item_name,
                description=auction_request.description,
                starting_price=auction_request.starting_price,
                duration_seconds=auction_request.duration_seconds,
                creator=user
            )

            return auction_pb2.StatusResponse(
                success=True,
                message=f"Auction created: {auction.auction_id}",
                auction_id = auction.auction_id
            )
        if request.HasField("place_bid"):
            bid_request = request.place_bid
            # Get the current auction before placing the new bid
            auction = self.auction_manager.get_auction(
                bid_request.auction_id
            )
            if auction is None:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="Auction not found"
                )
            previous_highest_bidder = auction.highest_bidder
            # Place the bid
            success, message, bid = self.auction_manager.place_bid(
                auction_id=bid_request.auction_id,
                bidder=user,
                amount=bid_request.amount
            )
            if not success:
                return auction_pb2.StatusResponse(
                    success=False,
                    message=message
                )

            # Reserve mock funds for the new highest bidder
            escrow_success, transaction_id = self.escrow.reserve_funds(
                bidder=user,
                auction_id=bid_request.auction_id,
                amount=bid_request.amount
            )

            if not escrow_success:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="Unable to reserve funds"
                )
            # Refund the previous highest bidder
            if previous_highest_bidder:
                self.escrow.refund_funds(
                    auction_id=bid_request.auction_id,
                    bidder=previous_highest_bidder
                )
            response = auction_pb2.StatusResponse(
                success=success,
                message=message
            )
            if bid is not None:
                response.auction_id = bid.auction_id
                return response
            return auction_pb2.StatusResponse(
                success=False,
                message="No valid operation specified")

        if request.HasField("close_auction"):
            auction_id = request.close_auction.auction_id
            auction = self.auction_manager.get_auction(auction_id)

            if auction is None:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="Auction not found"
                )

            if auction.creator != user:
                return auction_pb2.StatusResponse(
                    success=False,
                    message="You can only close auctions that you created"
                )
            success, msg = self.auction_manager.close_auction(auction_id)
            if not success:
                return auction_pb2.StatusResponse(
                    success=False,
                    message=msg,
                    auction_id=auction_id
                )

            # Release winner's mock funds
            if auction.winner:
                self.escrow.release_funds(
                    auction_id=auction_id,
                    winner=auction.winner
                )
            return auction_pb2.StatusResponse(
                success=success,
                message=msg,
                auction_id=auction_id
            )

        return auction_pb2.StatusResponse(
            success=False,
            message="No valid operation specified"
        )
    def Get(self, request, context):
        user = self.auth_service.validate_token(request.token)
        if user is None:
            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=False,
                    message="Not Authenticated"
                )
            )
        if request.HasField("get_auction"):
            auction_id = request.get_auction.auction_id

            with self.state_store.lock:
                auction = self.state_store.auctions.get(auction_id)

            if auction is None:
                return auction_pb2.GetResponse(
                    status=auction_pb2.StatusResponse(
                        success=False,
                        message="Auction not found"
                    )
                )

            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=True,
                    message="Auction found"
                ),
                auctions=[auction]
            )
        if request.HasField("get_auctions"):
            auctions = self.auction_manager.get_auctions(
                active_only=request.get_auctions.active_only
            )

            return auction_pb2.GetResponse(
                status=auction_pb2.StatusResponse(
                    success=True,
                    message="Auctions retrieved"
                ),
                auctions=auctions
            )
        if request.HasField("get_bids"):
            bids = self.auction_manager.get_bids(auction_id=request.get_bids.auction_id)
            return auction_pb2.GetResponse(
                status = auction_pb2.StatusResponse(
                    success=True,
                    message="Bids retrieved"
                ),
                bids=bids
            )

        return auction_pb2.GetResponse(
            status=auction_pb2.StatusResponse(
                success=False,
                message="No valid query specified"
            )
        )