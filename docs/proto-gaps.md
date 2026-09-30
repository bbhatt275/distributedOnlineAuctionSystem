# Proto gaps — proposed additions for Milestone 2

Written by the client team. **Nothing here blocks Milestone 1** — the client
works against `proto/auction.proto` exactly as it stands today. These are the
places where the current contract forces the client to compensate, and where a
small additive change would remove the compensation.

All five are backward compatible: new field numbers and new RPCs only, no
renumbering, no renames. A server that ignores them keeps working.

---

## 1. `StatusResponse` has no error code

```proto
message StatusResponse {
  bool success = 1;
  string message = 2;
  string auction_id = 3;
}
```

Every failure is a `bool` plus English prose. The client cannot branch on
behaviour without pattern-matching the prose, which it currently does in
[`client/errors.py`](../client/errors.py) against literals lifted from
`application/auction_manager.py`. If anyone rewords a message, the client
silently reclassifies that failure as `UNKNOWN` and stops retrying it.

This gets materially worse in Milestone 2, where the client must distinguish
"your bid was too low" (do not retry) from "this node is a follower" (retry
elsewhere immediately) from "quorum lost" (back off hard).

**Proposed**

```proto
enum StatusCode {
  STATUS_CODE_UNSPECIFIED = 0;
  OK = 1;
  INVALID_ARGUMENT = 2;
  UNAUTHENTICATED = 3;
  NOT_FOUND = 5;
  BID_TOO_LOW = 7;
  AUCTION_NOT_ACTIVE = 8;
  NOT_LEADER = 20;
  NO_QUORUM = 21;
  CONSENSUS_TIMEOUT = 22;
  INTERNAL = 99;
}

message StatusResponse {
  bool success = 1;          // keep: existing callers unaffected
  string message = 2;
  string auction_id = 3;
  StatusCode code = 4;       // new
  string leader_hint = 5;    // new: "host:port" when code == NOT_LEADER
  uint64 term = 6;           // new: Raft term the reply was served under
}
```

The client already parses a `leader_hint` out of the message string and
redirects on it (`test_leader_hint_redirects_without_round_robin`), so wiring
field 5 is a drop-in improvement.

---

## 2. No `idempotency_key` on writes

There is no way to tell the server "this is a resend, not a new request". A
write that fails *after* being applied is indistinguishable from one that never
landed, so the client cannot retry writes safely in general.

Current per-operation handling in [`client/resilience.py`](../client/resilience.py):

| Operation | Retry safe? | Why |
|---|---|---|
| `PlaceBid` | **yes, accidentally** | `auction_manager.place_bid` rejects `amount <= current_highest_bid`, so a resend of the same amount is a no-op. The client then reads back to see whether its "rejected" bid actually won. |
| `CloseAuction` | yes | sets `active=False`; idempotent |
| `Get*`, `Logout` | yes | reads / idempotent |
| `CreateAuction` | **no** | mints a fresh `uuid4` per call — a retry creates a duplicate auction |

`CreateAuction` is therefore retried only when the failure proves the request
never reached the server (connection refused), and otherwise reconciled by
listing auctions and matching on `(item_name, starting_price, start_time)` —
which is genuinely ambiguous if two users create the same item in the same
second.

**Proposed**: `string idempotency_key = N;` on `CreateAuctionRequest`,
`PlaceBidRequest` and `CloseAuctionRequest`, deduplicated server-side. In
Milestone 2 this key is also what makes a Raft log entry safe to re-propose
after a leader change.

---

## 3. `Auction` has no version

Nothing orders two snapshots of the same auction. The client currently infers
staleness from server invariants — highest bid never decreases while active,
auctions never reopen — implemented in `Store._is_stale` and covered by
`test_lower_highest_bid_on_active_auction_is_rejected_as_stale`.

That works for Milestone 1's single server. It is not sufficient once reads can
be served by a lagging follower, because a follower's snapshot can be *legally*
behind rather than merely out of order.

**Proposed**: `uint64 version = N;` on `Auction`, incremented on every mutation.
Also enables optimistic concurrency: `expected_version` on `PlaceBidRequest`.

---

## 4. No streaming RPC

`AuctionService` is entirely unary, so "real-time bid placement" has to be
synthesised. [`client/watcher.py`](../client/watcher.py) polls on an adaptive
interval (0.5 s when an auction is closing or recently active, 3 s otherwise)
and the store diffs snapshots into events, which the web UI receives over SSE.

It works and it demos well, but it is O(clients × auctions) RPCs against a
10-worker thread pool, and update latency is bounded below by the poll interval.

**Proposed**

```proto
rpc WatchAuctions(WatchAuctionsRequest) returns (stream AuctionEvent);
```

The client is already written so only `watcher.py` changes — the store, the UI
and the SSE bridge are unaware of how events are produced.

---

## 5. `double` money and second-resolution timestamps

```proto
double starting_price = 3;
double amount = 2;
int64 timestamp = 5;   // int(time.time()) — whole seconds
```

Two problems:

* **`double` for money.** Binary floating point cannot represent most decimal
  currency values exactly. Today the client only ever compares values that
  round-tripped unchanged through the wire, so it is safe — but any
  server-side arithmetic (increments, escrow totals, fee splits) can produce
  results that differ between replicas. Two Raft nodes applying the same log
  entry must reach byte-identical state.
* **Whole-second timestamps.** Far too coarse to order the concurrent bids the
  demo deliberately generates: 20 simultaneous bids share one timestamp. The
  client sorts bid history by amount instead (valid only because the server
  enforces strict monotonic increase) — see
  `test_bids_sorted_by_amount_not_timestamp`.

**Proposed**: integer minor units (paise) for money, and either
`int64 timestamp_unix_ms` or a monotonic per-auction sequence number for bids.

---

## Summary

| # | Gap | Client workaround today | Milestone 2 risk if unchanged |
|---|---|---|---|
| 1 | No error codes | prose pattern-matching | cannot distinguish follower / no-quorum / bad-bid |
| 2 | No idempotency key | per-op policy + read-back reconciliation | duplicate writes on leader failover |
| 3 | No version | infer from invariants | stale follower reads look fresh |
| 4 | No streaming | adaptive polling + SSE | poll load grows with cluster and clients |
| 5 | `double` money, 1 s timestamps | compare-only, sort by amount | replica divergence; unorderable bids |
