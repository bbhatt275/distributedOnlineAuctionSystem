from concurrent import futures

import grpc

from generated import auction_pb2_grpc
from application.grpc_service import AuctionService


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10)
    )

    auction_pb2_grpc.add_AuctionServiceServicer_to_server(
        AuctionService(),
        server
    )

    server.add_insecure_port("[::]:50051")

    server.start()

    print("Auction application server started on port 50051")

    server.wait_for_termination()


if __name__ == "__main__":
    serve()