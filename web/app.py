"""FastAPI + Jinja2 browser frontend.

    uvicorn web.app:app --reload --port 8000

The browser doesn't speak gRPC. It talks HTTP and SSE to this process, which
holds the gRPC client and forwards to the application server:

    browser --HTTP/SSE--> web.app --gRPC--> application server

Each browser session gets its own AuctionClient, so two tabs logged in as
different users behave like two separate client nodes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Cookie, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from client.auction_client import ASSISTANT_TASKS, AuctionClient
from client.errors import AuctionError
from client.watcher import AuctionWatcher

log = logging.getLogger("auction.web")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Distributed Auction System")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SESSION_COOKIE = "auction_session"


@dataclass
class BrowserSession:
    client: AuctionClient
    watcher: AuctionWatcher
    listeners: list

    def broadcast(self, event):
        payload = _event_json(event)
        if payload is None:
            return

        for q in list(self.listeners):
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Tab stopped draining. Drop the oldest rather than grow.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


_SESSIONS = {}


def _event_json(event):
    payload = {"type": event.type.value}
    p = event.payload

    if "auction" in p:
        payload["auction"] = vars(p["auction"])
    if "bid" in p:
        payload["bid"] = vars(p["bid"])

    for key in ("state", "detail", "message", "by", "amount", "username", "changed"):
        if key in p:
            payload[key] = p[key]

    return json.dumps(payload)


def _session(session_id):
    return _SESSIONS.get(session_id) if session_id else None


def _teardown(session_id):
    session = _SESSIONS.pop(session_id, None)
    if session is None:
        return

    try:
        session.watcher.stop()
        session.client.logout()
    except Exception:
        log.exception("error tearing down session")
    finally:
        session.client.close()


def _safe_refresh(client):
    """Refresh, tolerating an outage so the page still renders."""
    try:
        client.get_auctions(active_only=False)
    except AuctionError as exc:
        log.warning("refresh failed: %s", exc)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return RedirectResponse("/login", status_code=303)

    await asyncio.to_thread(_safe_refresh, session.client)

    return templates.TemplateResponse(request, "dashboard.html", {
        "username": session.client.session.username,
        "auctions": session.client.store.auctions(),
        "connection": session.client.store.connection.value,
        "health": session.client.pool.health(),
        "now": time.time(),
    })


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def do_login(response: Response, username: str = Form(...), password: str = Form(...)):
    client = AuctionClient()

    try:
        await asyncio.to_thread(client.login, username, password)
    except AuctionError as exc:
        client.close()
        return RedirectResponse(f"/login?error={exc.user_message()}", status_code=303)

    watcher = AuctionWatcher(client)
    watcher.attach_activity_tracking()

    session_id = secrets.token_urlsafe(24)
    session = BrowserSession(client=client, watcher=watcher, listeners=[])
    client.store.subscribe(session.broadcast)
    _SESSIONS[session_id] = session
    watcher.start()

    redirect = RedirectResponse("/", status_code=303)
    redirect.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return redirect


@app.post("/logout")
async def do_logout(auction_session: str | None = Cookie(default=None)):
    if auction_session:
        await asyncio.to_thread(_teardown, auction_session)

    redirect = RedirectResponse("/login", status_code=303)
    redirect.delete_cookie(SESSION_COOKIE)
    return redirect


@app.get("/auction/{auction_id}", response_class=HTMLResponse)
async def auction_detail(request: Request, auction_id: str,
                         auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return RedirectResponse("/login", status_code=303)

    session.watcher.focus(auction_id)

    auction = await asyncio.to_thread(session.client.get_auction, auction_id)
    if auction is None:
        return RedirectResponse("/", status_code=303)

    bids = await asyncio.to_thread(session.client.get_bids, auction_id)

    return templates.TemplateResponse(request, "auction.html", {
        "username": session.client.session.username,
        "auction": auction,
        "bids": bids,
        "connection": session.client.store.connection.value,
        "now": time.time(),
    })


@app.post("/auction/{auction_id}/bid")
async def place_bid(auction_id: str, amount: float = Form(...),
                    auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)

    try:
        outcome = await asyncio.to_thread(session.client.place_bid, auction_id, amount)
    except AuctionError as exc:
        return JSONResponse({"accepted": False, "message": exc.user_message()}, status_code=503)

    session.watcher.refresh_now()
    return JSONResponse({
        "accepted": outcome.accepted,
        "message": outcome.message,
        "recovered": outcome.recovered,
    })


@app.post("/auctions")
async def create_auction(item_name: str = Form(...), description: str = Form(""),
                         starting_price: float = Form(...), duration_seconds: int = Form(...),
                         auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return RedirectResponse("/login", status_code=303)

    try:
        await asyncio.to_thread(session.client.create_auction, item_name, description,
                                starting_price, duration_seconds)
    except AuctionError as exc:
        log.warning("create failed: %s", exc)

    session.watcher.refresh_now()
    return RedirectResponse("/", status_code=303)


@app.post("/auction/{auction_id}/close")
async def close_auction(auction_id: str, auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return RedirectResponse("/login", status_code=303)

    try:
        await asyncio.to_thread(session.client.close_auction, auction_id)
    except AuctionError as exc:
        log.warning("close failed: %s", exc)

    return RedirectResponse(f"/auction/{auction_id}", status_code=303)


@app.get("/api/state")
async def api_state(auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)

    return JSONResponse({
        **session.client.store.snapshot(),
        "health": session.client.pool.health(),
        "now": time.time(),
    })


@app.get("/api/assistant/tasks")
async def assistant_tasks(auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)

    return JSONResponse({
        "tasks": [{"id": t, "label": label, "hint": hint} for t, label, hint in ASSISTANT_TASKS],
        "connected": session.client.assistant_route_available(),
    })


@app.post("/api/assistant")
async def assistant_ask(task: str = Form(...), query: str = Form(...),
                        auction_id: str = Form(""),
                        auction_session: str | None = Cookie(default=None)):
    session = _session(auction_session)
    if session is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)

    try:
        reply = await asyncio.to_thread(
            session.client.ask_assistant, task, query, auction_id or None
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({
        "answer": reply.answer,
        "task": reply.task,
        "connected": reply.connected,
        "error": reply.error,
    })


@app.get("/events")
async def events(auction_session: str | None = Cookie(default=None)):
    """SSE bridge from the store to the browser."""
    session = _session(auction_session)
    if session is None:
        return JSONResponse({"error": "not logged in"}, status_code=401)

    listener = queue.Queue(maxsize=256)
    session.listeners.append(listener)

    async def stream():
        try:
            yield f"data: {json.dumps(session.client.store.snapshot())}\n\n"
            while True:
                try:
                    payload = await asyncio.to_thread(listener.get, True, 15.0)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            if listener in session.listeners:
                session.listeners.remove(listener)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.on_event("shutdown")
def shutdown():
    for session_id in list(_SESSIONS):
        _teardown(session_id)
