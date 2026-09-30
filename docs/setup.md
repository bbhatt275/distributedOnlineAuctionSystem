# Running the system

Milestone 1 runs four processes: Ollama, the LLM server, the application
server, and a client (CLI or web). Start them in that order — each depends on
the one before it.

## One-time setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Ollama, for the LLM node:

```bash
brew install ollama          # macOS; see ollama.com for Linux/Windows
ollama serve                 # leave running, listens on :11434
ollama pull llama3.2:3b      # or whatever LLM_MODEL names
```

The model must match `LLM_MODEL` in `llm/model.py`.

## Start the nodes

Four terminals. Ollama is already running from above.

```bash
# 1 — LLM server, :50052
.venv/bin/python -m llm.server

# 2 — application server, :50051
.venv/bin/python -m application.server

# 3 — web client, http://localhost:8000
.venv/bin/uvicorn web.app:app --port 8000

# 3b — or the CLI instead
.venv/bin/python -m client.commands --user ashish --password ashish123
```

Seed users are created in `application/auth_service.py`:
`ashish`, `bharat`, `chetna`, `devashish`,`shivesh` (passwords are `<name>123`, except
`bharat` / `password123`).

## Check it works

```bash
.venv/bin/python -m client.simulator race --clients 20
```

Should report `PASSED — no lost updates, no duplicate amounts, monotonic
increase held`.

In the web UI, open an auction, click **Ask AI**, pick a task and send. The
badge reads `live` when the assistant route is reachable, `not connected`
otherwise.

## Configuration

All optional — defaults work for a single-machine demo.

| Variable | Default | What it does |
|---|---|---|
| `AUCTION_ENDPOINTS` | `localhost:50051` | Comma-separated app servers. Multiple entries enable failover. |
| `APP_HOST` / `APP_PORT` | `localhost` / `50051` | Used when `AUCTION_ENDPOINTS` is unset. |
| `AUCTION_RPC_TIMEOUT` | `5.0` | Deadline for normal auction RPCs, seconds. |
| `AUCTION_LLM_TIMEOUT` | `90.0` | Deadline for `AskLLM`. Generation is much slower than a normal RPC. |
| `AUCTION_RETRY_ATTEMPTS` | `4` | Max attempts per operation. |
| `AUCTION_BREAKER_THRESHOLD` | `3` | Consecutive failures before an endpoint is taken out of rotation. |
| `AUCTION_BREAKER_RESET` | `5.0` | Seconds before an open breaker retries. |
| `AUCTION_POLL_FAST` / `AUCTION_POLL_IDLE` | `0.5` / `3.0` | Watcher poll interval, seconds. |
| `LLM_MODEL` | `llama3.2:3b` | Ollama model the LLM node loads. |

## Testing without Ollama

`tests/fakes/fake_llm_server.py` serves `llm.LLMService` on the same port with
deterministic answers built from the real `AuctionContext`. Useful for testing
the wiring on a machine that cannot run the model:

```bash
.venv/bin/python -m tests.fakes.fake_llm_server   # instead of llm.server
```

It verifies plumbing, not model quality.

## Tests

```bash
.venv/bin/python -m pytest tests/test_client_state.py tests/test_client_resilience.py -q
.venv/bin/python scripts/generate_proto.py --check
```

40 client tests, no servers required. The `--check` run fails if `generated/`
has drifted from `proto/`.

## Troubleshooting

**`ModuleNotFoundError: ollama`** — `pip install -r requirements.txt`.

**Assistant badge says "not connected"** — the application server is running a
build without the `AskLLM` RPC. Pull the latest and regenerate stubs.

**"The LLM node is not reachable"** — `llm.server` isn't running, or Ollama
isn't up. Check `curl localhost:11434/api/tags`.

**Assistant times out** — first call after a restart includes model load. Raise
`AUCTION_LLM_TIMEOUT`, or check `LLM_THINKING` isn't set to `1`.

**`GOAWAY ... ENHANCE_YOUR_CALM` in client logs** — a client keepalive tuned
more aggressively than the server tolerates. Current settings avoid it; see the
comment in `client/config.py::grpc_channel_options`.
