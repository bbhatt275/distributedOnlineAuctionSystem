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