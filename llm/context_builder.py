from generated import llm_pb2
from llm.prompts.faq import FAQ_SYSTEM_PROMPT
from llm.prompts.description import DESCRIPTION_SYSTEM_PROMPT
from llm.prompts.summary import SUMMARY_SYSTEM_PROMPT

def build_auction_context(auction):
    """
    Converts a structured AuctionContext Protobuf message into
    a single formatted string which is human-readable since that
    is how it is supposed to be as an input to LLM.

    The LLM receives only the information supplied by the
    Application Server.
    """

    if auction is None:
        return "No auction context was provided."

    parts = []

    # basic idea: check if field is available, then append it.
    if auction.auction_id:
        parts.append(f"Auction ID: {auction.auction_id}")

    if auction.item_name:
        parts.append(f"Item name: {auction.item_name}")

    if auction.item_description:
        parts.append(
            f"Item description: {auction.item_description}"
        )

    if auction.starting_price:
        parts.append(
            f"Starting price: {auction.starting_price}"
        )

    if auction.current_highest_bid:
        parts.append(
            f"Current highest bid: "
            f"{auction.current_highest_bid}"
        )

    if auction.currency:
        parts.append(
            f"Currency: {auction.currency}"
        )

    parts.append(
        f"Auction active: {auction.active}"
    )

    if auction.start_time:
        parts.append(
            f"Start time (Unix timestamp): "
            f"{auction.start_time}"
        )

    if auction.end_time:
        parts.append(
            f"End time (Unix timestamp): "
            f"{auction.end_time}"
        )

    if auction.bid_count:
        parts.append(
            f"Number of bids: {auction.bid_count}"
        )

    if auction.highest_bidder:
        parts.append(
            f"Highest bidder: {auction.highest_bidder}"
        )

    if auction.winner:
        parts.append(
            f"Winner: {auction.winner}"
        )

    if auction.winning_bid:
        parts.append(
            f"Winning bid: {auction.winning_bid}"
        )

    # Flexible item metadata.
    for key, value in auction.item_attributes.items():
        parts.append(f"{key}: {value}")

    # Optional individual bid history.
    if auction.bids:

        parts.append("Bid history:")

        for bid in auction.bids:
            parts.append(
                f"- Bid {bid.bid_id}: "
                f"{bid.amount} "
                f"by {bid.bidder} "
                f"at {bid.timestamp}"
            )

    if auction.additional_context:
        parts.append(
            f"Additional auction context: "
            f"{auction.additional_context}"
        )

    return "\n".join(parts)


def build_requester_context(requester):
    """
    Converts the optional requester information into text.
    """

    if requester is None:
        return ""

    parts = []
    #check for fields, if available then append.
    if requester.user_id:
        parts.append(
            f"Requester user ID: {requester.user_id}"
        )

    if requester.role:
        parts.append(
            f"Requester role: {requester.role}"
        )

    if requester.additional_context:
        parts.append(
            f"Requester context: "
            f"{requester.additional_context}"
        )

    return "\n".join(parts)


def build_messages(request):
    """
    Convert an LLMRequest protobuf message into the messages
    expected by the model layer.

    This function does NOT call Ollama.
    """

    auction_context = build_auction_context(
        request.auction_context
    )

    requester_context = build_requester_context(
        request.requester_context
    )

    context_sections = [
        "AUCTION CONTEXT:",
        auction_context,
    ]

    if requester_context:
        context_sections.extend([
            "",
            "REQUESTER CONTEXT:",
            requester_context,
        ])

    if request.additional_context:
        context_sections.extend([
            "",
            "ADDITIONAL CONTEXT:",
            request.additional_context,
        ])

    context_text = "\n".join(context_sections)

    messages = []

    # For previous conversation.
    for message in request.conversation_history:

        if message.role == llm_pb2.USER:
            role = "user"

        elif message.role == llm_pb2.ASSISTANT:
            role = "assistant"

        else:
            continue

        messages.append({
            "role": role,
            "content": message.content,
        })

    # Current request.
    messages.append({
        "role": "user",
        "content": (
            f"{context_text}\n\n"
            f"USER REQUEST:\n"
            f"{request.query}"
        ),
    })

    return messages

def get_system_prompt(task_type):
    """
    Select the system prompt based on the requested LLM task.
    """

    if task_type == llm_pb2.AUCTION_FAQ:
        return FAQ_SYSTEM_PROMPT

    if task_type == llm_pb2.ITEM_DESCRIPTION:
        return DESCRIPTION_SYSTEM_PROMPT

    if task_type in (
        llm_pb2.AUCTION_SUMMARY,
        llm_pb2.RESULT_SUMMARY,
    ):
        return SUMMARY_SYSTEM_PROMPT

    raise ValueError(
        f"Unsupported LLM task type: {task_type}"
    )