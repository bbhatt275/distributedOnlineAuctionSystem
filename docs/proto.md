# Proto contract reference

Documents the gRPC contract in `proto/`. This is the shared interface between
the client, the application server and the LLM server, so the field names here
are the ones all three must agree on.

Two files, two independent packages:

| File | Package | Server | Owner |
|---|---|---|---|
| `auction.proto` | `auction` | application server, :50051 | Person A |
| `llm.proto` | `llm` | LLM server, :50052 | Person C |

They deliberately do not import each other. The LLM service defines its own
`AuctionContext` rather than reusing `Auction`, so the application server
decides what auction information the model is allowed to see.

Regenerate stubs after any change:

```bash
python scripts/generate_proto.py           # writes generated/
python scripts/generate_proto.py --check   # CI: fails if stubs are stale
```

Stubs are committed, so nobody needs protoc just to run a node.

---

## auction.proto

### AuctionService

| RPC | Request | Response | Used by client for |
|---|---|---|---|
| `Login` | `LoginRequest` | `LoginResponse` | authentication |
| `Logout` | `LogoutRequest` | `StatusResponse` | ending a session |
| `Post` | `PostRequest` | `StatusResponse` | all writes |
| `Get` | `GetRequest` | `GetResponse` | all reads |
| `AskLLM` | `AskLLMRequest` | `AskLLMResponse` | assistant queries |

This matches the assignment's required client surface — `login`, `logout`,
`post`, `get`. Writes and reads are multiplexed through `Post`/`Get` using a
`oneof`, so adding an operation means adding a `oneof` arm, not a new RPC.

### Authentication

```proto
message LoginRequest  { string username = 1; string password = 2; }
message LoginResponse { StatusResponse status = 1; string token = 2; }
message LogoutRequest { string token = 1; }
```

The token is a 32-byte hex string (`secrets.token_hex(32)`) held in an
in-memory dict server-side. It carries no expiry field, so a client cannot
inspect a token to know whether it is still valid — it only finds out when a
call is rejected. Tokens do not survive a server restart.

Seed users live in `application/auth_service.py`: `bharat`, `alice`, `bob`,
`charlie`, `david`.

### Post — writes

```proto
message PostRequest {
  string token = 1;
  oneof operation {
    CreateAuctionRequest create_auction = 2;
    PlaceBidRequest      place_bid      = 3;
    CloseAuctionRequest  close_auction  = 4;
  }
}
```

| Arm | Fields | Notes |
|---|---|---|
| `create_auction` | `item_name`, `description`, `starting_price`, `duration_seconds` | server computes `start_time`/`end_time` from `duration_seconds`; mints a fresh uuid4 per call, so **not idempotent** |
| `place_bid` | `auction_id`, `amount` | rejected unless `amount > current_highest_bid` |
| `close_auction` | `auction_id` | idempotent; sets `active=false`, winner becomes current highest bidder |

All three return a bare `StatusResponse`. A bid does not return the created
`Bid` or the updated `Auction`, so the client issues a follow-up `Get` to
refresh state.

### Get — reads

```proto
message GetRequest {
  string token = 1;
  oneof query {
    GetAuctionRequest  get_auction  = 2;   // auction_id
    GetAuctionsRequest get_auctions = 3;   // active_only
    GetBidsRequest     get_bids     = 4;   // auction_id
  }
}

message GetResponse {
  StatusResponse  status   = 1;
  repeated Auction auctions = 2;
  repeated Bid     bids     = 3;
}
```

`GetResponse` carries both lists; whichever is irrelevant comes back empty.
`get_auction` returns a single-element `auctions` list rather than a scalar.


### AskLLM

```proto
import "llm.proto";

message AskLLMRequest {
  string token = 1;
  string query = 2;
  llm.LLMTaskType task_type = 3;
  string auction_id = 4;      // optional; attaches auction context
}

message AskLLMResponse {
  bool success = 1;
  string message = 2;
  string answer = 3;
}
```

The one place `auction.proto` imports `llm.proto` — `task_type` is the
`llm.LLMTaskType` enum, so clients send the enum value, not a string.

The application server owns the LLM hop: it validates the token, builds an
`llm.AuctionContext` (including bid history) when `auction_id` is supplied,
adds a `RequesterContext` naming the caller, and calls the LLM node. **The
client never dials the LLM server directly.**

`auction_id` is optional. Without it the model answers from the query alone —
appropriate for general FAQ questions.

Failure messages seen from this RPC:

| Message | Meaning |
|---|---|
| `Not authenticated` | bad or expired token |
| `Query cannot be empty` | blank query |
| `Auction not found` | unknown `auction_id` |
| `LLM server error: <repr>` | the LLM node failed or was unreachable |

The last one embeds the raw gRPC exception repr, which is unreadable in a chat
bubble; the client collapses the common cases in
`client/auction_client.py::_tidy_llm_error`. Returning a status code instead
would let that helper go away.

### Data messages

```proto
message Auction {
  string auction_id = 1;
  string item_name = 2;
  string description = 3;
  double starting_price = 4;
  double current_highest_bid = 5;    // seeded to starting_price, so a fresh
                                     // auction reads as if it has a bid
  string highest_bidder = 6;         // "" until someone bids
  int64  start_time = 7;             // unix seconds
  int64  end_time = 8;               // unix seconds
  bool   active = 9;
  string winner = 10;                // set when closed
}

message Bid {
  string bid_id = 1;                 // uuid4
  string auction_id = 2;
  string bidder = 3;                 // username
  double amount = 4;
  int64  timestamp = 5;              // unix seconds
}
```

`current_highest_bid` equals `starting_price` on a new auction, so it cannot
alone tell you whether bidding has started — check `highest_bidder != ""`.

### StatusResponse

```proto
message StatusResponse {
  bool   success = 1;
  string message = 2;
  string auction_id = 3;    // set on create, and on a successful bid
}
```

There is no error code — failures are a bool plus English prose. Messages the
client currently recognises, all literals from `application/`:

| Message | Meaning |
|---|---|
| `Invalid credentials` | bad username/password |
| `Not Authenticated` / `Not authenticated` | bad or expired token |
| `Auction not found` | unknown auction_id |
| `Bid must be higher than the current highest bid` | bid rejected |
| `Auction has ended` / `Auction is not active` | auction closed |
| `Starting price cannot be negative` | invalid create |
| `Duration must be positive` | invalid create |

Changing this wording breaks client error classification — see
`client/errors.py` and [proto-gaps.md](proto-gaps.md) item 1 for the proposed
status-code enum that would remove the string matching.

---

## llm.proto

Owned by Person C. The client never dials this service — the application
server calls it and injects auction context. Documented here for completeness.

### LLMService

```proto
rpc GetLLMAnswer(LLMRequest) returns (LLMResponse);
```

`LLMTaskType` selects the behaviour: `AUCTION_FAQ`, `ITEM_DESCRIPTION`,
`AUCTION_SUMMARY`, `RESULT_SUMMARY`.

`LLMRequest` carries `request_id`, `query`, an `AuctionContext`, optional
`RequesterContext`, optional `conversation_history`, and optional
`GenerationConfig` (`max_tokens`, `temperature`; 0 means server default).

`LLMResponse` returns `answer` plus `success`, a machine-readable
`LLMErrorCode`, `model_name`, `processing_time_ms` and optional token `usage`.

The LLM server is stateless — the application server owns conversation history
and passes it in. Note `llm.proto` does have an error-code enum; `auction.proto`
does not.

Reachable from the client via `AuctionService.AskLLM` (above), added in
`1ab5044`. The LLM node itself stays independent — it never imports
`auction.proto`, and receives only the context the application server chooses
to send.

---

## Conventions

- **proto3**, field numbers frozen once merged. Add new fields with new
  numbers; never renumber or reuse a retired number.
- Adding a field or a `oneof` arm is backward compatible. Renaming one is not.
- Money is `double` and timestamps are whole seconds. Both are workable for
  Milestone 1 but problematic for Raft — see [proto-gaps.md](proto-gaps.md)
  item 5.
- After editing a `.proto`, run `scripts/generate_proto.py` and commit the
  regenerated `generated/` alongside it, so the two never drift.

## Known gaps

Five backward-compatible additions are proposed in
[proto-gaps.md](proto-gaps.md), with the reasoning for each: status-code enum
plus leader hint, `idempotency_key` on writes, `version` on `Auction`, a
server-streaming watch RPC, and integer money with millisecond timestamps.

None of them block Milestone 1.
