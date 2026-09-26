import grpc

from generated import auction_pb2
from generated import auction_pb2_grpc


class AuctionClient:

    def __init__(self, host="localhost", port=50051):
        self.channel = grpc.insecure_channel(
            f"{host}:{port}"
        )

        self.stub = auction_pb2_grpc.AuctionServiceStub(
            self.channel
        )

    def login(self, username, password):
        request = auction_pb2.LoginRequest(
            username=username,
            password=password
        )

        return self.stub.Login(request)

    def create_auction(
            self,
            token,
            item_name,
            description,
            starting_price,
            duration_seconds
    ):
        request = auction_pb2.PostRequest(
            token=token,
            create_auction=auction_pb2.CreateAuctionRequest(
                item_name=item_name,
                description=description,
                starting_price=starting_price,
                duration_seconds=duration_seconds
            )
        )

        return self.stub.Post(request)

    def get_auction(self, token, auction_id):
        request = auction_pb2.GetRequest(
            token=token,
            get_auction=auction_pb2.GetAuctionRequest(
                auction_id=auction_id
            )
        )
        return self.stub.Get(request)

    def get_auctions(self,token,active_only=True):
        request = auction_pb2.GetRequest(
            token = token,
            get_auctions = auction_pb2.GetAuctionsRequest(
                active_only=active_only
            )
        )
        return self.stub.Get(request)

    def place_bid(self, token, auction_id, amount):
        request = auction_pb2.PostRequest(
            token=token,
            place_bid=auction_pb2.PlaceBidRequest(
                auction_id=auction_id,
                amount=amount
            )
        )
        return self.stub.Post(request)

    def get_bids(self, token, auction_id):
        request = auction_pb2.GetRequest(
            token=token,
            get_bids = auction_pb2.GetBidsRequest(
                auction_id=auction_id
            )
        )
        return self.stub.Get(request)

    def logout(self, token):
        request = auction_pb2.LogoutRequest(
            token = token
        )
        return self.stub.Logout(request)