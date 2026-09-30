from concurrent import futures

import grpc

from generated import llm_pb2_grpc
from llm.grpc_service import LLMService
from llm.model import MODEL_NAME


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4)
    )

    llm_pb2_grpc.add_LLMServiceServicer_to_server(
        LLMService(),
        server,
    )

    server.add_insecure_port("[::]:50052")

    server.start()

    print("======================================")
    print("        LLM gRPC SERVER")
    print("======================================")
    print(f"Model : {MODEL_NAME}")
    print("Address: 0.0.0.0:50052")
    print("Status : RUNNING")
    print("======================================")

    try:
        server.wait_for_termination()

    except KeyboardInterrupt:
        print("\nStopping LLM server...")
        server.stop(0)


if __name__ == "__main__":
    serve()