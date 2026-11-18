import os
from concurrent import futures

import grpc

from generated import auction_pb2_grpc, raft_pb2_grpc
from application.grpc_service import AuctionService
from application.state_store import StateStore
from raft.raft_node import RaftNode
from raft.election import ElectionManager
from raft.raft_service import RaftService


def serve():
    node_id = os.getenv("NODE_ID", "1")
    port = int(os.getenv("PORT", "50051"))

    all_peers = {
        "1": "node1:50051",
        "2": "node2:50051",
        "3": "node3:50051",
        "4": "node4:50051",
        "5": "node5:50051",
    }
    peers = {
        peer_id: address
        for peer_id, address in all_peers.items()
        if peer_id != node_id
    }

    # One state store per node, shared by the gRPC service and Raft callback.
    state_store = StateStore()
    raft_node = RaftNode(node_id, peers)
    election_manager = ElectionManager(raft_node)

    auction_service = AuctionService(
        raft_node=raft_node,
        election_manager=election_manager,
        state_store=state_store,
    )

    # This is the application state-machine callback.
    # Register the state-machine callback and replay durable committed entries
    # before this node starts accepting client traffic or participating in elections.
    raft_node.set_apply_callback(auction_service.apply_command)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10)
    )

    auction_pb2_grpc.add_AuctionServiceServicer_to_server(
        auction_service, server
    )
    raft_pb2_grpc.add_RaftServiceServicer_to_server(
        RaftService(raft_node), server
    )

    server.add_insecure_port(f"[::]:{port}")
    server.start()

    print("=" * 38)
    print(f" AUCTION NODE {node_id}")
    print(f" Port: {port}")
    print(f" Peers: {peers}")
    print(" Status: RUNNING")
    print("=" * 38)

    raft_node.running = True
    election_manager.start()

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print(f"Stopping Node {node_id}...")
    finally:
        raft_node.running = False
        server.stop(0)


if __name__ == "__main__":
    serve()
