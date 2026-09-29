from generated import llm_pb2

from llm.generator import LLMGenerator


def create_auction_request(task_type, query):

    return llm_pb2.LLMRequest(
        request_id="generation-test-001",

        task_type=task_type,

        query=query,

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
                "condition": "Used",
            },
        ),
    )


def run_test(task_type, query):

    generator = LLMGenerator()

    request = create_auction_request(
        task_type,
        query,
    )

    answer = generator.generate(request)

    print("\n======================================")
    print("TASK:", task_type)
    print("QUERY:", query)
    print("======================================")
    print(answer)


def main():

    # --------------------------------------------------------
    # FAQ
    # --------------------------------------------------------

    run_test(
        llm_pb2.AUCTION_FAQ,

        "What is the current highest bid?",
    )


    # --------------------------------------------------------
    # ITEM DESCRIPTION
    # --------------------------------------------------------

    run_test(
        llm_pb2.ITEM_DESCRIPTION,

        "Generate a professional auction listing description "
        "for this item.",
    )


    # --------------------------------------------------------
    # AUCTION SUMMARY
    # --------------------------------------------------------

    run_test(
        llm_pb2.AUCTION_SUMMARY,

        "Summarize the current auction.",
    )


if __name__ == "__main__":
    main()