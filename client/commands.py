"""Interactive CLI for the auction client.

    python -m client.commands                      # interactive shell
    python -m client.commands --user alice --password alice123

Live updates are pushed into the terminal by the watcher, so a bid placed by
another client (or the web UI, or the simulator) appears here without the user
typing anything.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shlex
import sys
import time

from client.auction_client import AuctionClient
from client.errors import AuctionError
from client.state import ConnectionState, Event, EventType
from client.watcher import AuctionWatcher

# ANSI colours, disabled when stdout is redirected.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


DIM = lambda s: _c("2", s)        # noqa: E731
BOLD = lambda s: _c("1", s)       # noqa: E731
RED = lambda s: _c("31", s)       # noqa: E731
GREEN = lambda s: _c("32", s)     # noqa: E731
YELLOW = lambda s: _c("33", s)    # noqa: E731
CYAN = lambda s: _c("36", s)      # noqa: E731

_CONN_STYLE = {
    ConnectionState.CONNECTED: GREEN,
    ConnectionState.CONNECTING: YELLOW,
    ConnectionState.DEGRADED: YELLOW,
    ConnectionState.PARTITIONED: RED,
    ConnectionState.DISCONNECTED: DIM,
}


def money(value: float) -> str:
    return f"Rs {value:,.2f}"


def remaining(seconds: int) -> str:
    if seconds <= 0:
        return "ended"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class AuctionShell:
    def __init__(self, client: AuctionClient, watcher: AuctionWatcher) -> None:
        self.client = client
        self.watcher = watcher
        self.running = True
        self._live = True  # whether to print pushed events

    # -- event rendering ------------------------------------------------------

    def on_event(self, event: Event) -> None:
        if not self._live:
            return
        p = event.payload
        me = self.client.session.username

        if event.type is EventType.BID_PLACED:
            bid = p["bid"]
            who = "you" if bid.bidder == me else bid.bidder
            print(f"\n  {CYAN('[bid]')} {money(bid.amount)} by {who} on {bid.auction_id[:8]}")
        elif event.type is EventType.OUTBID:
            a = p["auction"]
            print(f"\n  {RED('[outbid]')} {a.item_name}: {p['by']} now leads at {money(p['amount'])}")
        elif event.type is EventType.AUCTION_CLOSED:
            a = p["auction"]
            winner = a.winner or "no winner"
            print(f"\n  {YELLOW('[closed]')} {a.item_name} -> {winner} at {money(a.current_highest_bid)}")
        elif event.type is EventType.AUCTION_ADDED:
            a = p["auction"]
            print(f"\n  {DIM('[new]')} {a.item_name} ({a.auction_id[:8]}) from {money(a.starting_price)}")
        elif event.type is EventType.CONNECTION_CHANGED:
            state = ConnectionState(p["state"])
            if state is not ConnectionState.CONNECTED:
                style = _CONN_STYLE.get(state, DIM)
                print(f"\n  {style('[' + state.value + ']')} {p.get('detail') or ''}")

    # -- commands -------------------------------------------------------------

    def cmd_help(self, *_: str) -> None:
        print(
            """
  auctions [all]        list auctions (default: active only)
  show <id>             auction detail + bid history, follows live
  create                create an auction (prompts)
  bid <id> <amount>     place a bid
  close <id>            close an auction
  ask <question>        ask the AI assistant
  whoami                current session and cluster health
  live [on|off]         toggle pushed live updates
  help                  this text
  quit                  logout and exit

  <id> may be an 8-character prefix.
"""
        )

    def _resolve(self, prefix: str) -> str | None:
        """Accept an auction id prefix for convenience."""
        matches = [a.auction_id for a in self.client.store.auctions() if a.auction_id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            print(RED(f"  no auction matching {prefix!r}"))
        else:
            print(RED(f"  {prefix!r} is ambiguous ({len(matches)} matches)"))
        return None

    def cmd_auctions(self, *args: str) -> None:
        active_only = not (args and args[0] == "all")
        self.client.get_auctions(active_only=False)
        rows = self.client.store.auctions(active_only=active_only)
        if not rows:
            print(DIM("  no auctions"))
            return

        now = time.time()
        print(f"\n  {'ID':<10}{'ITEM':<22}{'CURRENT':>14}  {'LEADER':<12}{'ENDS':>8}")
        print(DIM("  " + "-" * 68))
        for a in rows:
            leader = a.highest_bidder or DIM("--")
            state = remaining(a.seconds_remaining(now)) if a.active else RED("closed")
            print(f"  {a.auction_id[:8]:<10}{a.item_name[:21]:<22}{money(a.current_highest_bid):>14}  {leader:<12}{state:>8}")
        print()

    def cmd_show(self, *args: str) -> None:
        if not args:
            print(RED("  usage: show <id>"))
            return
        auction_id = self._resolve(args[0])
        if not auction_id:
            return

        auction = self.client.get_auction(auction_id)
        if auction is None:
            print(RED("  auction not found"))
            return
        self.watcher.focus(auction_id)
        bids = self.client.get_bids(auction_id)

        print(f"\n  {BOLD(auction.item_name)}   {DIM(auction.auction_id)}")
        print(f"  {auction.description}")
        print(f"  start {money(auction.starting_price)}   current {BOLD(money(auction.current_highest_bid))}")
        print(f"  leader: {auction.highest_bidder or '--'}   "
              f"status: {'active, ' + remaining(auction.seconds_remaining(time.time())) if auction.active else 'closed, winner ' + (auction.winner or '--')}")
        if bids:
            print(f"\n  {'BIDDER':<12}{'AMOUNT':>14}   TIME")
            print(DIM("  " + "-" * 44))
            for b in bids:
                stamp = time.strftime("%H:%M:%S", time.localtime(b.timestamp))
                print(f"  {b.bidder:<12}{money(b.amount):>14}   {DIM(stamp)}")
        else:
            print(DIM("\n  no bids yet"))
        print()

    def cmd_create(self, *args: str) -> None:
        try:
            item = input("  item name: ").strip()
            desc = input("  description: ").strip()
            price = float(input("  starting price: ").strip())
            duration = int(input("  duration (seconds): ").strip())
        except (ValueError, EOFError):
            print(RED("  cancelled"))
            return

        result = self.client.create_auction(item, desc, price, duration)
        if result.created:
            note = DIM(" (recovered after ambiguous reply)") if result.recovered else ""
            print(GREEN(f"  created {result.auction_id[:8]}") + note)
        else:
            print(RED(f"  {result.message}"))

    def cmd_bid(self, *args: str) -> None:
        if len(args) < 2:
            print(RED("  usage: bid <id> <amount>"))
            return
        auction_id = self._resolve(args[0])
        if not auction_id:
            return
        try:
            amount = float(args[1])
        except ValueError:
            print(RED("  amount must be a number"))
            return

        outcome = self.client.place_bid(auction_id, amount)
        if outcome.accepted:
            note = DIM(" (confirmed by read-back)") if outcome.recovered else ""
            print(GREEN(f"  bid accepted at {money(amount)}") + note)
        else:
            print(RED(f"  rejected: {outcome.message}"))
        self.watcher.refresh_now()

    def cmd_close(self, *args: str) -> None:
        if not args:
            print(RED("  usage: close <id>"))
            return
        auction_id = self._resolve(args[0])
        if not auction_id:
            return
        self.client.close_auction(auction_id)
        auction = self.client.store.auction(auction_id)
        print(GREEN(f"  closed; winner: {(auction.winner if auction else None) or '--'}"))

    def cmd_ask(self, *args: str) -> None:
        # The LLM node is another team member's scope and proto/auction.proto
        # exposes no route to it, so there is nothing to call yet. Kept as a
        # visible stub so the UI surface is ready when they wire it up.
        if not args:
            print(RED("  usage: ask <question>"))
            return
        print(YELLOW("  [assistant] not wired up yet."))
        print(DIM("  proto/auction.proto has no LLM route; proto/llm.proto is served by the"))
        print(DIM("  separate LLM node and the app server has not exposed a passthrough RPC."))

    def cmd_whoami(self, *_: str) -> None:
        session = self.client.session
        conn = self.client.store.connection
        style = _CONN_STYLE.get(conn, DIM)
        print(f"\n  user:       {session.username or DIM('not logged in')}")
        print(f"  connection: {style(conn.value)}")
        print(f"  preferred:  {self.client.pool.preferred_endpoint}")
        for endpoint, breaker in self.client.pool.health().items():
            mark = GREEN("ok") if breaker == "closed" else RED(breaker)
            print(f"    {endpoint:<24}{mark}")
        if err := self.client.store.last_error:
            print(f"  last error: {DIM(err)}")
        print()

    def cmd_live(self, *args: str) -> None:
        if args:
            self._live = args[0] == "on"
        print(f"  live updates {'on' if self._live else 'off'}")

    def cmd_quit(self, *_: str) -> None:
        self.running = False

    # -- loop -----------------------------------------------------------------

    COMMANDS = {
        "help": "cmd_help", "?": "cmd_help",
        "auctions": "cmd_auctions", "ls": "cmd_auctions",
        "show": "cmd_show",
        "create": "cmd_create",
        "bid": "cmd_bid",
        "close": "cmd_close",
        "ask": "cmd_ask",
        "whoami": "cmd_whoami",
        "live": "cmd_live",
        "quit": "cmd_quit", "exit": "cmd_quit",
    }

    def run(self) -> None:
        self.cmd_help()
        while self.running:
            try:
                raw = input(f"{CYAN(self.client.session.username or 'auction')}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue

            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                print(RED(f"  {exc}"))
                continue

            handler = self.COMMANDS.get(parts[0].lower())
            if handler is None:
                print(RED(f"  unknown command {parts[0]!r}; try 'help'"))
                continue

            try:
                getattr(self, handler)(*parts[1:])
            except AuctionError as exc:
                print(RED(f"  {exc.user_message()}"))
                logging.getLogger("auction.cli").debug("command failed", exc_info=exc)
            except Exception:
                logging.getLogger("auction.cli").exception("unexpected error")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Distributed auction system CLI client")
    parser.add_argument("--user", help="username (prompted if omitted)")
    parser.add_argument("--password", help="password (prompted if omitted)")
    parser.add_argument("--endpoints", help="comma-separated app server endpoints")
    parser.add_argument("--verbose", "-v", action="store_true", help="show retry/failover logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="  %(levelname)s %(name)s: %(message)s",
    )

    config = None
    if args.endpoints:
        from client.config import ClientConfig

        config = ClientConfig(endpoints=[e.strip() for e in args.endpoints.split(",")])

    client = AuctionClient(config=config)

    username = args.user or input("username: ").strip()
    password = args.password or getpass.getpass("password: ")

    try:
        client.login(username, password)
    except AuctionError as exc:
        print(RED(f"login failed: {exc.user_message()}"))
        return 1

    print(GREEN(f"\nlogged in as {username}") + DIM(f"  via {client.pool.preferred_endpoint}"))

    watcher = AuctionWatcher(client)
    watcher.attach_activity_tracking()
    shell = AuctionShell(client, watcher)
    client.store.subscribe(shell.on_event)

    with watcher:
        try:
            shell.run()
        finally:
            client.logout()
            client.close()

    print(DIM("bye"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
