from client.grpc_client import AuctionClient


def main():
    client = AuctionClient()

    response = client.login(
        username="bharat",
        password="password123"
    )

    print("Success:", response.status.success)
    print("Message:", response.status.message)
    print("Token:", response.token)


if __name__ == "__main__":
    main()