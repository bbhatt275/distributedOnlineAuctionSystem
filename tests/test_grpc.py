import grpc

from generated import llm_pb2
from generated import llm_pb2_grpc


def main():

    # Connect to the independent LLM server
    channel = grpc.insecure_channel("localhost:50052")

    stub = llm_pb2_grpc.LLMServiceStub(channel)

    # Build an LLM request
    request = llm_pb2.LLMRequest(
        request_id="grpc-test-001",

        task_type=llm_pb2.AUCTION_FAQ,

        query="What is the current highest bid?",

        auction_context=llm_pb2.AuctionContext(
            auction_id="A102",
            item_name="Lenovo ThinkPad T14",
            item_description=(
                "Used business laptop in good working condition."
            ),
            starting_price=30000,
            current_highest_bid=45000,
            currency="INR",
            active=True,
            bid_count=14,
            highest_bidder="user42",
            item_attributes={
                "processor": "Ryzen 7 Pro",
                "RAM": "16 GB",
                "storage": "512 GB SSD",
                "condition": "Used",
            },
        ),
    )

    # Make the actual gRPC call
    response = stub.GetLLMAnswer(request)

    print("\n======================================")
    print("        gRPC RESPONSE")
    print("======================================")

    print("Request ID       :", response.request_id)
    print("Success          :", response.success)
    print("Answer           :", response.answer)
    print("Message          :", response.message)
    print("Error Code       :", response.error_code)
    print("Model            :", response.model_name)
    print("Processing Time  :", response.processing_time_ms, "ms")

    print("======================================")


if __name__ == "__main__":
    main()