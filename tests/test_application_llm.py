from generated import llm_pb2

from application.llm_client import LLMClient


def main():

    client = LLMClient(
        "localhost:50052"
    )

    auction_context = llm_pb2.AuctionContext(
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
    )

    response = client.get_answer(
        request_id="application-test-001",

        task_type=llm_pb2.AUCTION_FAQ,

        query="What is the current highest bid?",

        auction_context=auction_context,
    )

    print("\n======================================")
    print(" APPLICATION → LLM RESPONSE")
    print("======================================")

    print("Request ID      :", response.request_id)
    print("Success         :", response.success)
    print("Answer          :", response.answer)
    print("Message         :", response.message)
    print("Error Code      :", response.error_code)
    print("Model           :", response.model_name)
    print(
        "Processing Time :",
        response.processing_time_ms,
        "ms"
    )

    print("======================================")


if __name__ == "__main__":
    main()