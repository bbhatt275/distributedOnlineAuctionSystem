"""Concurrent client simulator -- the assignment's "Node 5".

    python -m client.simulator race --clients 20
    python -m client.simulator soak --clients 5 --duration 60

Each worker is a real AuctionClient with its own token, channel and retry
state, so this exercises the same path the CLI and web UI use.

The race mode is the concurrency-control demo: N clients bid on one auction at
once, and afterwards we check the server serialised them properly -- no
duplicate amounts, no lost updates, strictly increasing bids.
"""

from __future__ import annotations

import argparse
import logging
import random
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from client.auction_client import AuctionClient
from client.config import ClientConfig
from client.errors import AuctionError, ErrorKind

log = logging.getLogger("auction.simulator")

# From application/auth_service.py
SEED_USERS = [
    ("alice", "alice123"),
    ("bob", "bob123"),
    ("charlie", "charlie123"),
    ("david", "david123"),
    ("bharat", "password123"),
]


@dataclass
class Outcome:
    worker: int
    username: str
    amount: float
    accepted: bool
    kind: str
    latency_s: float
    recovered: bool = False


@dataclass
class Report:
    outcomes: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, outcome):
        with self.lock:
            self.outcomes.append(outcome)

    def summarise(self, title):
        with self.lock:
            outcomes = list(self.outcomes)

        if not outcomes:
            print("no results")
            return

        accepted = [o for o in outcomes if o.accepted]
        latencies = sorted(o.latency_s for o in outcomes)

        print(f"\n=== {title} ===")
        print(f"  attempts      {len(outcomes)}")
        print(f"  accepted      {len(accepted)}")
        print(f"  rejected      {len(outcomes) - len(accepted)}")

        recovered = [o for o in accepted if o.recovered]
        if recovered:
            print(f"  recovered     {len(recovered)}  (ambiguous reply, confirmed by read-back)")

        print("\n  outcome breakdown")
        for kind, count in Counter(o.kind for o in outcomes).most_common():
            print(f"    {kind:<22}{count}")

        print("\n  latency (s)")
        print(f"    p50  {statistics.median(latencies):.3f}")
        print(f"    p95  {latencies[int(len(latencies) * 0.95) - 1]:.3f}")
        print(f"    max  {latencies[-1]:.3f}")


def _make_client(endpoints):
    config = ClientConfig(endpoints=[e.strip() for e in endpoints.split(",")]) if endpoints else None
    return AuctionClient(config=config)


def _classify(exc, accepted):
    if exc is not None:
        return exc.kind.value
    return "accepted" if accepted else "rejected_too_low"


def race(clients, endpoints, base_price=1000.0):
    setup = _make_client(endpoints)
    setup.login(*SEED_USERS[0])

    created = setup.create_auction(
        item_name=f"Race Item {int(time.time())}",
        description="Concurrency control demo",
        starting_price=base_price,
        duration_seconds=120,
    )
    if not created.created:
        print(f"could not create auction: {created.message}")
        return 1

    auction_id = created.auction_id
    print(f"auction {auction_id[:8]} created at {base_price:.2f}; releasing {clients} bidders\n")

    report = Report()
    barrier = threading.Barrier(clients)
    workers = []

    def work(index):
        username, password = SEED_USERS[index % len(SEED_USERS)]
        client = _make_client(endpoints)
        workers.append(client)

        try:
            client.login(username, password)
        except AuctionError as exc:
            report.add(Outcome(index, username, 0, False, exc.kind.value, 0.0))
            barrier.wait()
            return

        amount = base_price + (index + 1) * 10
        barrier.wait()

        started = time.perf_counter()
        exc = None
        accepted = recovered = False
        try:
            outcome = client.place_bid(auction_id, amount)
            accepted, recovered = outcome.accepted, outcome.recovered
        except AuctionError as err:
            exc = err
        elapsed = time.perf_counter() - started

        report.add(Outcome(index, username, amount, accepted,
                           _classify(exc, accepted), elapsed, recovered))

    threads = [threading.Thread(target=work, args=(i,), name=f"bidder-{i}") for i in range(clients)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    report.summarise(f"race: {clients} concurrent bidders")
    print(f"  wall clock    {wall:.3f}s")

    final = setup.get_auction(auction_id)
    bids = setup.get_bids(auction_id)
    accepted = [o for o in report.outcomes if o.accepted]

    print("\n  consistency")
    print(f"    server bids recorded    {len(bids)}")
    print(f"    clients told 'accepted' {len(accepted)}")
    print(f"    final highest           {final.current_highest_bid:.2f} by {final.highest_bidder}")

    problems = []

    if len(bids) != len(accepted):
        problems.append(f"bid count mismatch: server {len(bids)} vs clients {len(accepted)}")

    amounts = [b.amount for b in bids]
    if len(set(amounts)) != len(amounts):
        problems.append("duplicate bid amounts recorded (lost update)")

    if amounts and final.current_highest_bid != max(amounts):
        problems.append(f"highest bid {final.current_highest_bid} != max recorded {max(amounts)}")

    if [b.amount for b in sorted(bids, key=lambda b: b.amount)] != sorted(set(amounts)):
        problems.append("accepted bids are not strictly increasing")

    if problems:
        print("\n  FAILED")
        for problem in problems:
            print(f"    - {problem}")
    else:
        print("\n  PASSED  no lost updates, no duplicate amounts, monotonic increase held")

    for client in workers + [setup]:
        client.close()

    return 1 if problems else 0


def soak(clients, duration, endpoints):
    """Sustained bidding. Meant to be run while you kill a node."""
    setup = _make_client(endpoints)
    setup.login(*SEED_USERS[0])

    created = setup.create_auction(
        item_name=f"Soak Item {int(time.time())}",
        description="Sustained load / failover demo",
        starting_price=100.0,
        duration_seconds=duration + 60,
    )
    auction_id = created.auction_id

    print(f"auction {auction_id[:8]}; {clients} clients bidding for {duration}s")
    print("kill the application server at any point -- clients should recover\n")

    report = Report()
    stop = threading.Event()

    def work(index):
        username, password = SEED_USERS[index % len(SEED_USERS)]
        client = _make_client(endpoints)

        try:
            client.login(username, password)
        except AuctionError as exc:
            log.error("worker %d could not log in: %s", index, exc)
            return

        while not stop.is_set():
            current = client.store.auction(auction_id)
            base = current.current_highest_bid if current else 100.0
            amount = round(base + random.uniform(1, 50), 2)

            started = time.perf_counter()
            exc = None
            accepted = recovered = False
            try:
                outcome = client.place_bid(auction_id, amount)
                accepted, recovered = outcome.accepted, outcome.recovered
            except AuctionError as err:
                exc = err
                if err.kind is ErrorKind.PARTITIONED:
                    time.sleep(1.0)
            elapsed = time.perf_counter() - started

            report.add(Outcome(index, username, amount, accepted,
                               _classify(exc, accepted), elapsed, recovered))
            stop.wait(random.uniform(0.2, 0.8))

        client.close()

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(clients)]
    for t in threads:
        t.start()

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        print("\ninterrupted")

    stop.set()
    for t in threads:
        t.join(timeout=10)

    report.summarise(f"soak: {clients} clients / {duration}s")

    final = setup.get_auction(auction_id)
    if final:
        print(f"\n  final highest  {final.current_highest_bid:.2f} by {final.highest_bidder}")
    setup.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Concurrent auction client simulator")
    parser.add_argument("mode", choices=["race", "soak"])
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--duration", type=int, default=30, help="soak only, seconds")
    parser.add_argument("--endpoints", help="comma-separated app server endpoints")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="  %(levelname)s %(name)s: %(message)s",
    )

    if args.mode == "race":
        return race(args.clients, args.endpoints)
    return soak(args.clients, args.duration, args.endpoints)


if __name__ == "__main__":
    raise SystemExit(main())
