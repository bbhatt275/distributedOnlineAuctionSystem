import grpc

from application import auction_manager
from generated import auction_pb2
from generated import auction_pb2_grpc
from application.auth_service import AuthService
from application.auction_manager import AuctionManager
from application.state_store import StateStore

class AuctionService(auction_pb2_grpc.AuctionServiceServicer):
    def __init__(self):
        self.auth_service = AuthService()
        self.state_store = StateStore()
        self.auction_manager = AuctionManager(self.state_store)

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
                mesage = "Not authenticated"
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

            success, message, bid = self.auction_manager.place_bid(
                auction_id=bid_request.auction_id,
                bidder=user,
                amount=bid_request.amount
            )
            response = auction_pb2.StatusResponse(
                success=success,
                message=message
            )
            if bid is not None:
                response.auction_id = bid.auction_id
            return response

        if request.HasField("close_auction"):
            success, msg = self.auction_manager.close_auction(request.close_auction.auction_id)
            return auction_pb2.StatusResponse(
                success = success,
                message = msg
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