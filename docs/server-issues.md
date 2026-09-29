# Server issues found while building the client

Filed by the client team against `origin/develop` (`d6be3c9`). Server code is
not our scope, so none of these are fixed here — but the client hits all of
them, so they are documented with reproductions.

---

## 1. `Post` crashes instead of rejecting an invalid token — **one-character fix**

`application/grpc_service.py`, `Post()`:

```python
if user is None:
    return auction_pb2.StatusResponse(
        success=False,
        mesage = "Not authenticated"      # <-- "mesage", should be "message"
    )
```

`StatusResponse` has no field `mesage`, so protobuf raises `ValueError` inside
the servicer. Instead of a clean `success=False`, the caller gets an `UNKNOWN`
gRPC error.

**Reproduction**

```
$ python -c "
from client.grpc_client import AuctionClient
c = AuctionClient()
print(c.get_auctions('bad-token').status.message)   # Get: clean
print(c.place_bid('bad-token', 'x', 1.0))           # Post: raises
"
Not Authenticated
grpc._channel._InactiveRpcError: StatusCode.UNKNOWN
  details = 'Exception calling application: Protocol message StatusResponse has no "mesage" field.'
```

`Get()` handles this correctly; only `Post()` is affected.

**Impact** Every write with an expired token surfaces as a server crash rather
than an auth failure. The client special-cases this string in
`client/errors.py::classify_rpc_error` so re-authentication still works — that
workaround should be deleted once this is fixed.

---

## 2. No authorization checks on auction operations

`Post()` validates *that* the caller has a token, never *who* they are:

* **Anyone can close anyone's auction.** `CloseAuctionRequest` carries only
  `auction_id`; `close_auction` never compares the caller to the seller. Any
  logged-in user can end a competitor's auction early and freeze the current
  highest bid as the winner.
* **Sellers can bid on their own auctions.** Nothing compares `bidder` to the
  auction's creator. (`client/client.py` does exactly this in its demo flow.)
* **`creator` is captured but never stored.** `auction_manager.create_auction`
  takes a `creator` argument and drops it — the `Auction` message has no seller
  field, so ownership cannot be checked even in principle.

**Suggested** add `string seller_id` to `Auction` in `proto/auction.proto`,
populate it from the validated token, and reject `CloseAuction` from non-sellers
with `PERMISSION_DENIED` and self-bids with `SELF_BID_FORBIDDEN`. See
[proto-gaps.md](proto-gaps.md) item 1 for the status-code enum.

---

## 3. LLM integration -- resolved

Fixed on feature/application-auction (1ab5044, "integrated llm"). The server
now exposes a dedicated RPC rather than an arm on PostRequest:

```proto
rpc AskLLM(AskLLMRequest) returns (AskLLMResponse);
```

`AskLLM` authenticates the token, builds an `llm.AuctionContext` (including bid
history) when an `auction_id` is supplied, adds a `RequesterContext`, and calls
`LLMClient`. `AskLLMResponse` carries a real `answer` field, so the answer no
longer has to travel in `message`.

The client is wired to it and the full chain is verified working:
client -> web -> application server -> LLM node.

One rough edge: when the LLM node is down, `AskLLM` returns
`"LLM server error: <_InactiveRpcError of RPC that terminated with: ...>"` --
the raw repr of the gRPC exception. It is unreadable in a chat bubble, so the
client collapses the common cases in `_tidy_llm_error`. Returning the gRPC
status code instead of `str(e)` would let that helper go away.

## 4. Minor

* **`escrow.py` and `auction_service.py` are empty files.** Milestone 1 lists
  mock escrow as a deliverable; the client has no escrow surface to call.
* **`GetBids` on an unknown auction returns `None`**, not an empty list.
  `state_store.bids.get(auction_id)` returns `None`, which protobuf accepts as
  "unset" — so the reply is `success=True` with no bids, indistinguishable from
  a real auction that has none. A missing auction should arguably be
  `NOT_FOUND`.
* **`requirements.txt` was missing `python-multipart`**, which FastAPI needs for
  form handling. Added on this branch, along with the missing trailing newline.

---

## Verified as *not* problems

* **`track_auction` lock re-entry.** The auction-expiry thread takes
  `state_store.lock` and then calls `get_auctions()` / `close_auction()`, which
  take it again. This would deadlock with a plain `threading.Lock`, but
  `StateStore` uses `threading.RLock`, so it is correct. Worth a comment, since
  changing that one word would deadlock every active auction.
* **Concurrency control on bids.** Verified with 20 simultaneous bidders via
  `python -m client.simulator race --clients 20`: no lost updates, no duplicate
  amounts, strict monotonic increase held.
