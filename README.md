# Distributed Online Auction System

Advanced Operating Systems (CS G623) project — a distributed auction system
built on gRPC, with a domain-specific LLM on its own node, and Raft consensus
in Milestone 2.

Python throughout, as the assignment requires. All inter-service communication
is gRPC.

## Architecture

Milestone 1 — one application server, LLM on a separate node:

```
        browser
           │ HTTP + SSE
           ▼
    ┌──────────────┐
    │  web client  │   FastAPI + Jinja2
    └──────┬───────┘
           │ gRPC
           ▼
    ┌──────────────────────┐        ┌──────────────┐
    │ application server   │──gRPC─▶│  LLM server  │──▶ Ollama
    │ auth · auctions      │        │   (Ollama)   │
    │ bidding · timer      │        └──────────────┘
    └──────────────────────┘
           ▲
           │ gRPC
    CLI client · load simulator
```

The browser never speaks gRPC — the web process holds the gRPC client and
bridges over HTTP/SSE, which avoids gRPC-Web and an Envoy proxy. The client
never dials the LLM node directly; the application server owns that hop and
decides what auction context the model sees.

Milestone 2 replaces the single application server with a Raft cluster behind
the same client API.

## Components

| Path | What it is |
|---|---|
| `proto/` | The shared contract. `auction.proto` (client ↔ app server), `llm.proto` (app server ↔ LLM). |
| `generated/` | gRPC stubs, committed so nobody needs protoc to run a node. |
| `application/` | Application server — authentication, auctions, bidding, auction timer, LLM passthrough. |
| `llm/` | Standalone LLM server: prompt construction, task routing, Ollama inference. |
| `client/` | Client library — resilient gRPC client, state store, CLI, load simulator. |
| `web/` | Browser frontend and the assistant widget. |
| `tests/` | Unit tests, plus a fake LLM node for testing without Ollama. |
| `docs/` | Documentation (below). |

## Running it

See **[docs/setup.md](docs/setup.md)** for the full walkthrough. Short version:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ollama && ollama serve && ollama pull <model>

.venv/bin/python -m llm.server                 # :50052
.venv/bin/python -m application.server         # :50051
.venv/bin/uvicorn web.app:app --port 8000      # http://localhost:8000
```

Seed users are in `application/auth_service.py`.

## Documentation

| Document | Contents |
|---|---|
| [docs/setup.md](docs/setup.md) | Install, run, configure, troubleshoot |
| [docs/proto.md](docs/proto.md) | The gRPC contract, field by field |
| [docs/client.md](docs/client.md) | Client architecture, retry and failure handling |
| [docs/proto-gaps.md](docs/proto-gaps.md) | Proposed proto changes for Milestone 2 |
| [docs/server-issues.md](docs/server-issues.md) | Known server-side issues, with reproductions |

## Milestone 1 status

| Deliverable | State |
|---|---|
| gRPC service definitions | done |
| Client–server communication | done — CLI, web, simulator |
| Authentication and sessions | done — bcrypt, token sessions, transparent re-login |
| Auction lifecycle | done — create, bid, timer-driven close, winner |
| Concurrency control | done — verified with 20 simultaneous bidders |
| LLM integration | done — four task types, context-aware |
| Project structure and setup | done |
| Mock escrow | done — reserve, release, refund on auction close |

Concurrency is checked by the simulator rather than asserted:

```bash
.venv/bin/python -m client.simulator race --clients 20
```

It fires N simultaneous bids at one auction and then verifies the server
serialised them — no duplicate amounts, no lost updates, strictly increasing
bids.

## Testing

```bash
.venv/bin/python -m pytest tests/test_client_state.py tests/test_client_resilience.py -q
.venv/bin/python scripts/generate_proto.py --check
```

## Team

| Area | Scope |
|---|---|
| Application server | `proto/auction.proto`, `application/` — auctions, bidding, auth, LLM passthrough |
| LLM server | `llm/` — model, prompts, task routing |
| Client and frontend | `client/`, `web/` — resilient client, CLI, browser UI, simulator |

`proto/`, `generated/`, `docs/` and CI are shared. Work goes through feature
branches and pull requests into `develop`.
