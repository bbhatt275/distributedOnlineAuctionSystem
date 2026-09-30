import llm
from client.grpc_client import AuctionClient


def main():
    client = AuctionClient()

    # ============================================================
    # 1. LOGIN
    # ============================================================

    login_response = client.login(
        "bharat",
        "password123"
    )

    print("\n=== LOGIN ===")
    print("Success:", login_response.status.success)
    print("Message:", login_response.status.message)

    if not login_response.status.success:
        return

    token = login_response.token
    print("Token:", token)

    # ============================================================
    # 2. CREATE AUCTION
    # ============================================================

    create_response = client.create_auction(
        token=token,
        item_name="Gaming Laptop",
        description="High performance gaming laptop",
        starting_price=50000,
        duration_seconds=300
    )

    print("\n=== CREATE AUCTION ===")
    print("Success:", create_response.success)
    print("Message:", create_response.message)
    print("Auction ID:", create_response.auction_id)

    if not create_response.success:
        return

    auction_id = create_response.auction_id

    # ============================================================
    # 3. GET AUCTION
    # ============================================================

    get_response = client.get_auction(
        token,
        auction_id
    )

    print("\n=== GET AUCTION ===")
    print("Success:", get_response.status.success)

    if get_response.status.success:
        auction = get_response.auctions[0]

        print("Auction ID:", auction.auction_id)
        print("Item:", auction.item_name)
        print("Description:", auction.description)
        print("Starting price:", auction.starting_price)
        print("Current highest bid:", auction.current_highest_bid)
        print("Highest bidder:", auction.highest_bidder)
        print("Active:", auction.active)

    # ============================================================
    # 4. PLACE FIRST BID
    # ============================================================

    bid_response = client.place_bid(
        token=token,
        auction_id=auction_id,
        amount=55000
    )

    print("\n=== BID 1 ===")
    print("Success:", bid_response.success)
    print("Message:", bid_response.message)

    # ============================================================
    # 5. TRY INVALID LOWER BID
    # ============================================================

    bid_response = client.place_bid(
        token=token,
        auction_id=auction_id,
        amount=52000
    )

    print("\n=== BID 2 — LOWER BID ===")
    print("Success:", bid_response.success)
    print("Message:", bid_response.message)

    # ============================================================
    # 6. PLACE HIGHER BID
    # ============================================================

    print("\n=== CLOSE AUCTION ===")
    res = client.close_auction(token, auction_id)

    bid_response = client.place_bid(
        token=token,
        auction_id=auction_id,
        amount=60000
    )

    print("\n=== BID 3 — HIGHER BID ===")
    print("Success:", bid_response.success)
    print("Message:", bid_response.message)

    # ============================================================
    # 7. GET AUCTION AGAIN
    # ============================================================

    get_response = client.get_auction(
        token,
        auction_id
    )

    print("\n=== FINAL AUCTION STATE ===")

    if get_response.status.success:
        auction = get_response.auctions[0]

        print("Current highest bid:", auction.current_highest_bid)
        print("Highest bidder:", auction.highest_bidder)
        print("Active:", auction.active)

    # ============================================================
    # 8. GET BIDS
    # ============================================================

    bids_response = client.get_bids(
        token,
        auction_id
    )

    print("\n=== ALL BIDS ===")
    print("Success:", bids_response.status.success)

    for bid in bids_response.bids:
        print(
            "Bid ID:", bid.bid_id,
            "| Bidder:", bid.bidder,
            "| Amount:", bid.amount,
            "| Time:", bid.timestamp
        )

    print("\n=== WATCHER TEST ===")

    res = client.create_auction(
        token,
        "Watcher Test Item",
        "Testing automatic auction expiry",
        100.0,
        5  # 5 seconds
    )

    auction_id = res.auction_id

    print("Auction created:", auction_id)

    res = client.place_bid(token, auction_id, 150.0)

    print("Bid:", res.success, res.message)

    #llm test
    from generated import llm_pb2

    response = client.ask_llm(
        token,
        "What is the current highest bid?",
        llm_pb2.AUCTION_FAQ,
        auction_id
    )

    print(response.answer)

    import time

    print("Waiting for auction to expire...")
    time.sleep(7)

    res = client.get_auction(token, auction_id)

    auction = res.auctions[0]

    print("\n=== AFTER EXPIRY ===")
    print("Active:", auction.active)
    print("Winner:", auction.winner)
    print("Highest bid:", auction.current_highest_bid)



    # ============================================================
    # 9. LOGOUT
    # ============================================================

    logout_response = client.logout(token)

    print("\n=== LOGOUT ===")
    print("Success:", logout_response.success)
    print("Message:", logout_response.message)


if __name__ == "__main__":
    main()