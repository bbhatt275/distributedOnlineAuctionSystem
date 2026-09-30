"""Interactive CLI client.

    python -m client.commands
    python -m client.commands --user alice --password alice123

The watcher pushes updates into the terminal, so a bid placed by another
client shows up here without typing anything.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shlex
import sys
import time

from client.auction_client import ASSISTANT_TASKS, AuctionClient
from client.errors import AuctionError
from client.state import ConnectionState, EventType
from client.watcher import AuctionWatcher

_TTY = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def DIM(s): return _c("2", s)
def BOLD(s): return _c("1", s)
def RED(s): return _c("31", s)
def GREEN(s): return _c("32", s)
def YELLOW(s): return _c("33", s)
def CYAN(s): return _c("36", s)


_CONN_STYLE = {
    ConnectionState.CONNECTED: GREEN,
    ConnectionState.CONNECTING: YELLOW,
    ConnectionState.DEGRADED: YELLOW,
    ConnectionState.PARTITIONED: RED,
    ConnectionState.DISCONNECTED: DIM,
}


def money(value):
    return f"Rs {value:,.2f}"


def remaining(seconds):
    if seconds <= 0:
        return "ended"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class AuctionShell:

    def __init__(self, client, watcher):
        self.client = client
        self.watcher = watcher
        self.running = True
        self._live = True
        self._ask_task = ASSISTANT_TASKS[0][0]

    def on_event(self, event):
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
            print(f"\n  {YELLOW('[closed]')} {a.item_name} won by {a.winner or 'nobody'} "
                  f"at {money(a.current_highest_bid)}")

        elif event.type is EventType.AUCTION_ADDED:
            a = p["auction"]
            print(f"\n  {DIM('[new]')} {a.item_name} ({a.auction_id[:8]}) from {money(a.starting_price)}")

        elif event.type is EventType.CONNECTION_CHANGED:
            state = ConnectionState(p["state"])
            if state is not ConnectionState.CONNECTED:
                style = _CONN_STYLE.get(state, DIM)
                print(f"\n  {style('[' + state.value + ']')} {p.get('detail') or ''}")

    def cmd_help(self, *_):
        print("""
  auctions [all]        list auctions (default: active only)
  show <id>             auction detail + bid history
  create                create an auction (prompts)
  bid <id> <amount>     place a bid
  close <id>            close an auction
  ask [<id>] <question> ask the assistant about an auction
  task [<name>]         show or choose the assistant task
  whoami                session and cluster health
  live [on|off]         toggle live updates
  help                  this text
  quit                  logout and exit

  <id> may be an 8-character prefix.
""")

    def _resolve(self, prefix):
        matches = [a.auction_id for a in self.client.store.auctions()
                   if a.auction_id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            print(RED(f"  no auction matching {prefix!r}"))
        else:
            print(RED(f"  {prefix!r} is ambiguous ({len(matches)} matches)"))
        return None

    def cmd_auctions(self, *args):
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
            print(f"  {a.auction_id[:8]:<10}{a.item_name[:21]:<22}"
                  f"{money(a.current_highest_bid):>14}  {leader:<12}{state:>8}")
        print()

    def cmd_show(self, *args):
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

        status = ("active, " + remaining(auction.seconds_remaining(time.time()))
                  if auction.active else "closed, winner " + (auction.winner or "--"))

        print(f"\n  {BOLD(auction.item_name)}   {DIM(auction.auction_id)}")
        print(f"  {auction.description}")
        print(f"  start {money(auction.starting_price)}   "
              f"current {BOLD(money(auction.current_highest_bid))}")
        print(f"  leader: {auction.highest_bidder or '--'}   status: {status}")

        if bids:
            print(f"\n  {'BIDDER':<12}{'AMOUNT':>14}   TIME")
            print(DIM("  " + "-" * 44))
            for b in bids:
                stamp = time.strftime("%H:%M:%S", time.localtime(b.timestamp))
                print(f"  {b.bidder:<12}{money(b.amount):>14}   {DIM(stamp)}")
        else:
            print(DIM("\n  no bids yet"))
        print()

    def cmd_create(self, *args):
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

    def cmd_bid(self, *args):
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

    def cmd_close(self, *args):
        if not args:
            print(RED("  usage: close <id>"))
            return

        auction_id = self._resolve(args[0])
        if not auction_id:
            return

        self.client.close_auction(auction_id)
        auction = self.client.store.auction(auction_id)
        print(GREEN(f"  closed; winner: {(auction.winner if auction else None) or '--'}"))

    def cmd_ask(self, *args):
        if not args:
            print(RED("  usage: ask [<id>] <question>"))
            print(DIM("  tasks: " + ", ".join(t for t, _, _ in ASSISTANT_TASKS)))
            return

        # The first word is treated as an auction id when it matches one that
        # is already known, so that the assistant can be asked about a
        # specific item without naming a task.
        auction_id = None
        words = list(args)
        if len(words) > 1:
            candidate = self._resolve_quiet(words[0])
            if candidate:
                auction_id = candidate
                words = words[1:]

        task = self._ask_task
        query = " ".join(words)

        try:
            reply = self.client.ask_assistant(task, query, auction_id)
        except ValueError as exc:
            print(RED(f"  {exc}"))
            return

        if reply.error:
            print(RED(f"  {reply.error}"))
            return

        if not reply.connected:
            print(YELLOW("  assistant is not available on this server"))

        print()
        for line in (reply.answer or "").splitlines():
            print(f"  {line}")
        print()

    def cmd_task(self, *args):
        """Choose which assistant task subsequent questions use."""
        names = [t for t, _, _ in ASSISTANT_TASKS]

        if not args:
            for name, label, hint in ASSISTANT_TASKS:
                mark = GREEN(" *") if name == self._ask_task else "  "
                print(f"  {mark} {name:<18}{label} ({hint})")
            return

        choice = args[0].upper()
        if choice not in names:
            print(RED(f"  unknown task {args[0]!r}"))
            return

        self._ask_task = choice
        print(GREEN(f"  assistant task set to {choice}"))

    def _resolve_quiet(self, prefix):
        """Resolve an auction id prefix, returning None instead of reporting."""
        matches = [a.auction_id for a in self.client.store.auctions()
                   if a.auction_id.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def cmd_whoami(self, *_):
        session = self.client.session
        conn = self.client.store.connection
        style = _CONN_STYLE.get(conn, DIM)

        print(f"\n  user:       {session.username or DIM('not logged in')}")
        print(f"  connection: {style(conn.value)}")
        print(f"  preferred:  {self.client.pool.preferred_endpoint}")
        for endpoint, breaker in self.client.pool.health().items():
            mark = GREEN("ok") if breaker == "closed" else RED(breaker)
            print(f"    {endpoint:<24}{mark}")
        if self.client.store.last_error:
            print(f"  last error: {DIM(self.client.store.last_error)}")
        print()

    def cmd_live(self, *args):
        if args:
            self._live = args[0] == "on"
        print(f"  live updates {'on' if self._live else 'off'}")

    def cmd_quit(self, *_):
        self.running = False

    COMMANDS = {
        "help": "cmd_help", "?": "cmd_help",
        "auctions": "cmd_auctions", "ls": "cmd_auctions",
        "show": "cmd_show",
        "create": "cmd_create",
        "bid": "cmd_bid",
        "close": "cmd_close",
        "ask": "cmd_ask",
        "task": "cmd_task",
        "whoami": "cmd_whoami",
        "live": "cmd_live",
        "quit": "cmd_quit", "exit": "cmd_quit",
    }

    def run(self):
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
            except Exception:
                logging.getLogger("auction.cli").exception("unexpected error")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Distributed auction system CLI client")
    parser.add_argument("--user")
    parser.add_argument("--password")
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
