import grpc

from generated import auction_pb2
from generated import auction_pb2_grpc


class AuctionService(auction_pb2_grpc.AuctionServiceServicer):

    def Login(self, request, context):
        print(f"Login request received for: {request.username}")

        return auction_pb2.LoginResponse(
            status=auction_pb2.StatusResponse(
                success=True,
                message="Login endpoint is working"
            ),
            token="test-token"
        )

    def Logout(self, request, context):
        print(f"Logout request received for token: {request.token}")

        return auction_pb2.StatusResponse(
            success=True,
            message="Logout endpoint is working"
        )

    def Post(self, request, context):
        print("Post request received")

        return auction_pb2.StatusResponse(
            success=True,
            message="Post endpoint is working"
        )

    def Get(self, request, context):
        print("Get request received")

        return auction_pb2.GetResponse(
            status=auction_pb2.StatusResponse(
                success=True,
                message="Get endpoint is working"
            )
        )