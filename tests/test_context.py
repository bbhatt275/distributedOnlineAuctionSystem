from generated import llm_pb2

from llm.context_builder import build_messages


def main():

    request = llm_pb2.LLMRequest(
        request_id="test-context-001",

        task_type=llm_pb2.AUCTION_SUMMARY,

        query="Summarize the current auction.",

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
            start_time=1790000000,
            end_time=1790007200,
            bid_count=14,
            highest_bidder="user42",
            item_attributes={
                "processor": "Ryzen 7 Pro",
                "RAM": "16 GB",
                "storage": "512 GB SSD",
            },
        ),
    )

    messages = build_messages(request)

    print("\n===== GENERATED MESSAGES =====\n")

    for message in messages:
        print(f"ROLE: {message['role']}")
        print(message["content"])
        print("\n-------------------------------\n")


if __name__ == "__main__":
    main()