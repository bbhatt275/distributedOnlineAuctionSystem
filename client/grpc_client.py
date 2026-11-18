from dataclasses import replace

from generated import auction_pb2
from client.config import CONFIG
from client.errors import classify_status
from client.resilience import (
    AT_MOST_ONCE,
    IDEMPOTENT,
    NodePool,
    call,
)


class AuctionClient:
    """Auction gRPC client with endpoint rotation and leader-aware failover.

    AUCTION_ENDPOINTS must list host endpoints in Raft node-ID order:
    node 1 first, node 2 second, and so on.
    """

    def __init__(self, host=None, port=None, endpoints=None, config=None):
        if config is not None:
            client_config = config
        elif endpoints is not None:
            client_config = replace(CONFIG, endpoints=list(endpoints))
        elif host is not None:
            client_config = replace(
                CONFIG,
                endpoints=[f"{host}:{port or 50051}"],
            )
        else:
            client_config = CONFIG

        self.config = client_config
        self.pool = NodePool(config=client_config)

    def _invoke(self, policy, method, request, status_field=None, timeout=None):
        rpc_timeout = timeout or self.config.rpc_timeout_s

        def invoke(stub, endpoint):
            response = getattr(stub, method)(request, timeout=rpc_timeout)
            if status_field == "status":
                status = response.status
                if not status.success:
                    self._raise_if_cluster_or_auth_error(
                        status.message, endpoint, allow_invalid_login=(method == "Login")
                    )
            elif status_field == "self":
                if not response.success:
                    self._raise_if_cluster_or_auth_error(
                        response.message, endpoint
                    )
            return response

        return call(self.pool, policy, invoke, config=self.config)

    @staticmethod
    def _raise_if_cluster_or_auth_error(
        message, endpoint, allow_invalid_login=False
    ):
        error = classify_status(message, endpoint=endpoint)
        # Preserve the original response-based API for ordinary validation
        # failures (e.g. a bid that is too low). Only control-flow failures
        # need to escape so NodePool can redirect/retry or ask for re-login.
        from client.errors import ErrorKind
        if allow_invalid_login and (message or "").strip().lower() == "invalid credentials":
            return
        if error.kind in {
            ErrorKind.NOT_LEADER,
            ErrorKind.NO_QUORUM,
            ErrorKind.CONSENSUS_TIMEOUT,
            ErrorKind.UNAUTHENTICATED,
        }:
            raise error

    def login(self, username, password):
        request = auction_pb2.LoginRequest(
            username=username,
            password=password,
        )
        return self._invoke(
            IDEMPOTENT("Login"), "Login", request, status_field="status"
        )

    def create_auction(
        self, token, item_name, description, starting_price, duration_seconds
    ):
        request = auction_pb2.PostRequest(
            token=token,
            create_auction=auction_pb2.CreateAuctionRequest(
                item_name=item_name,
                description=description,
                starting_price=starting_price,
                duration_seconds=duration_seconds,
            ),
        )
        return self._invoke(
            AT_MOST_ONCE("CreateAuction"), "Post", request, status_field="self"
        )

    def get_auction(self, token, auction_id):
        request = auction_pb2.GetRequest(
            token=token,
            get_auction=auction_pb2.GetAuctionRequest(auction_id=auction_id),
        )
        return self._invoke(
            IDEMPOTENT("GetAuction"), "Get", request, status_field="status"
        )

    def get_auctions(self, token, active_only=True):
        request = auction_pb2.GetRequest(
            token=token,
            get_auctions=auction_pb2.GetAuctionsRequest(active_only=active_only),
        )
        return self._invoke(
            IDEMPOTENT("GetAuctions"), "Get", request, status_field="status"
        )

    def place_bid(self, token, auction_id, amount):
        request = auction_pb2.PostRequest(
            token=token,
            place_bid=auction_pb2.PlaceBidRequest(
                auction_id=auction_id,
                amount=amount,
            ),
        )
        return self._invoke(
            AT_MOST_ONCE("PlaceBid"), "Post", request, status_field="self"
        )

    def get_bids(self, token, auction_id):
        request = auction_pb2.GetRequest(
            token=token,
            get_bids=auction_pb2.GetBidsRequest(auction_id=auction_id),
        )
        return self._invoke(
            IDEMPOTENT("GetBids"), "Get", request, status_field="status"
        )

    def logout(self, token):
        request = auction_pb2.LogoutRequest(token=token)
        return self._invoke(
            IDEMPOTENT("Logout"), "Logout", request, status_field="self"
        )

    def close_auction(self, token, auction_id):
        request = auction_pb2.PostRequest(
            token=token,
            close_auction=auction_pb2.CloseAuctionRequest(auction_id=auction_id),
        )
        return self._invoke(
            AT_MOST_ONCE("CloseAuction"), "Post", request, status_field="self"
        )

    def ask_llm(self, token, query, task_type, auction_id=""):
        request = auction_pb2.AskLLMRequest(
            token=token,
            query=query,
            task_type=task_type,
            auction_id=auction_id,
        )
        return self._invoke(
            AT_MOST_ONCE("AskLLM"),
            "AskLLM",
            request,
            status_field="self",
            timeout=self.config.llm_timeout_s,
        )

    def endpoint_health(self):
        return self.pool.health()

    def close(self):
        self.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
