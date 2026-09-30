# Client architecture

Milestone 1 client for the Distributed Online Auction System. Python only,
gRPC to the application server, per the assignment's technical requirements.

## Running

See [setup.md](setup.md) for the full four-process run, including Ollama.

```bash
.venv/bin/python -m llm.server                  # LLM node
.venv/bin/python -m application.server          # application server
.venv/bin/uvicorn web.app:app --port 8000       # browser UI
.venv/bin/python -m client.commands             # or the CLI
.venv/bin/python -m client.simulator race --clients 20   # concurrency demo
```

## Layout

```
client/
  config.py          endpoints, timeouts, retry/breaker/poll knobs (env-overridable)
  errors.py          AuctionError + ErrorKind; maps gRPC codes and server prose
  resilience.py      retry loop, circuit breaker, node pool, failover
  auth_client.py     SessionManager — token lifecycle, transparent re-login
  state.py           observable Store — diffing, staleness guards, events
  auction_client.py  resilient facade used by every surface
  watcher.py         adaptive polling that synthesises real-time events
  commands.py        interactive CLI
  simulator.py       concurrent client simulator ("Node 5")
  grpc_client.py     pre-existing thin stub wrapper (unchanged)
tests/fakes/
  fake_llm_server.py stand-in LLM node for testing without Ollama
web/
  app.py             FastAPI + Jinja2; browser <-HTTP/SSE-> web <-gRPC-> server
```

The browser never speaks gRPC — `web/app.py` holds the gRPC client and bridges
over HTTP + Server-Sent Events, which avoids gRPC-Web and an Envoy proxy.

## Layering

```
  CLI / web UI / simulator
            │
      AuctionClient        per-operation retry policy, ambiguity resolution
       │          │
  SessionManager  Store    token lifecycle │ observable state + diffing
            │
       resilience          retry · backoff+jitter · breaker · failover
            │
      generated stubs  ──gRPC──>  application server
```

`Store` is the single source of truth. The CLI subscribes to it to print live
events; `web/app.py` subscribes to it to push SSE frames. Neither knows that
updates come from polling rather than a stream.

## Real-time updates

`proto/auction.proto` has no server-streaming RPC, so `watcher.py` polls and
the store diffs snapshots into `BID_PLACED` / `AUCTION_UPDATED` / `OUTBID` /
`AUCTION_CLOSED` events. The interval adapts:

| condition | interval |
|---|---|
| auction ends within 30 s | 0.5 s |
| anything changed in the last 10 s | 0.5 s |
| otherwise | 3 s |

Bid history is only polled for auctions the UI has explicitly focused, so cost
does not grow with the total number of auctions.


## Assistant widget

A floating panel on every logged-in page, offering the four `llm.LLMTaskType`
values as chips — FAQ, item description, auction summary, result summary.
Selecting a chip changes the input hint; on an auction page the widget passes
that `auction_id` automatically, so answers are grounded in real state.

```
browser  --POST /api/assistant-->  web.app  --AskLLM-->  app server  -->  LLM node
```

The task list comes from `GET /api/assistant/tasks` rather than being hardcoded
in the template, so it is defined once in `ASSISTANT_TASKS`.

`assistant_route_available()` checks whether the server exposes `AskLLM`. When
it doesn't, the widget shows a red *not connected* badge and renders replies
with a dashed border, so a stub can't be mistaken for a real answer during a
demo. It turns itself on when the RPC appears.

Two behaviours differ from normal auction RPCs:

* **`AskLLM` is at-most-once.** A timeout may mean the model is still
  generating; resending queues another expensive run behind it. Only failures
  that prove nothing was sent are retried.
* **It gets its own deadline**, `llm_timeout_s` (90s), because generation takes
  seconds to tens of seconds where an auction RPC takes milliseconds.

## Failure handling

Classified in `errors.py`, acted on in `resilience.py`:

| situation | client behaviour |
|---|---|
| node unreachable | retry with exponential backoff + full jitter, fail over to the next endpoint |
| every node unreachable | surfaced as `PARTITIONED`, connection pill turns red |
| node flapping | circuit breaker opens after 3 consecutive failures, half-open probe after 5 s |
| `NOT_LEADER` + leader hint | redirect straight to the hinted leader, no round-robin |
| token expired | re-login once and replay, collapsing concurrent refreshes via a generation counter |
| bad credentials / bid too low | non-retryable, fails immediately |
| servicer crash | classified `SERVER_BUG`, not retried |
| LLM node down | reported in the widget, never retried |

Retry policy is **per operation**, because the proto has no idempotency key —
see [proto-gaps.md](proto-gaps.md) item 2 for the full table and reasoning.
`PlaceBid` is safe to retry because the server rejects non-increasing bids;
`CreateAuction` is not, and is reconciled by read-back instead.

Full jitter on backoff matters for the simulator: without it, N clients that
lose a node retry in lockstep and thunder its replacement.

## Tests

```bash
.venv/bin/python -m pytest tests/test_client_state.py tests/test_client_resilience.py -q
```

40 tests, no server required — the resilience tests drive a fake stub that
raises scripted gRPC errors.

## Milestone 2 readiness

Already in place: multi-endpoint node pool, failover, `NOT_LEADER` handling and
leader-hint redirect, `PARTITIONED` detection, per-endpoint breaker state shown
in the UI. Point `AUCTION_ENDPOINTS` at a comma-separated list of Raft peers and
the client spreads across them:

```bash
AUCTION_ENDPOINTS=node1:50051,node2:50052,node3:50053 .venv/bin/python -m client.commands
```

Not yet possible: the server reports no role, term or commit index, so the
client cannot prefer an up-to-date follower for reads or show which node is
leader. That needs a `Health` RPC — proto-gaps item 1.
