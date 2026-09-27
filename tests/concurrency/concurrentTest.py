from concurrent.futures import ThreadPoolExecutor, as_completed

from client.grpc_client import AuctionClient


def place_bid(username, password, auction_id, amount):
    client = AuctionClient()

    # Login
    login_response = client.login(username, password)

    if not login_response.status.success:
        return {
            "username": username,
            "amount": amount,
            "success": False,
            "message": "Login failed"
        }

    token = login_response.token

    # Place bid
    response = client.place_bid(
        token=token,
        auction_id=auction_id,
        amount=amount
    )

    return {
        "username": username,
        "amount": amount,
        "success": response.success,
        "message": response.message
    }


def main():

    # ------------------------------------------------------------
    # Create an auction using one client
    # ------------------------------------------------------------

    client = AuctionClient()

    login_response = client.login(
        "bharat",
        "password123"
    )

    if not login_response.status.success:
        print("Main login failed")
        return

    token = login_response.token

    create_response = client.create_auction(
        token=token,
        item_name="Gaming Laptop",
        description="Concurrent bidding test",
        starting_price=50000,
        duration_seconds=300
    )

    if not create_response.success:
        print("Auction creation failed")
        return

    auction_id = create_response.auction_id

    print("Auction created")
    print("Auction ID:", auction_id)
    print("Starting price: ₹50,000")

    # ------------------------------------------------------------
    # Bidders
    # ------------------------------------------------------------

    bidders = [
        ("alice", "alice123", 55000),
        ("bob", "bob123", 60000),
        ("charlie", "charlie123", 57000),
        ("david", "david123", 65000),
        ("bharat", "password123", 62000),
    ]
    # ------------------------------------------------------------
    # Send bids concurrently
    # ------------------------------------------------------------

    print("\nSending concurrent bids...\n")

    with ThreadPoolExecutor(max_workers=5) as executor:

        futures = [
            executor.submit(
                place_bid,
                username,
                password,
                auction_id,
                amount
            )
            for username, password, amount in bidders
        ]

        for future in as_completed(futures):
            result = future.result()

            print(
                f"Bid ₹{result['amount']} "
                f"→ {result['success']} "
                f"({result['message']})"
            )

    # ------------------------------------------------------------
    # Check final auction state
    # ------------------------------------------------------------

    final_response = client.get_auction(
        token,
        auction_id
    )

    print("\n=== FINAL AUCTION STATE ===")

    if final_response.status.success:

        auction = final_response.auctions[0]

        print(
            "Final highest bid:",
            f"₹{auction.current_highest_bid}"
        )

        print(
            "Highest bidder:",
            auction.highest_bidder
        )

    else:
        print(
            "Could not retrieve auction:",
            final_response.status.message
        )


if __name__ == "__main__":
    main()