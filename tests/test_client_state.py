"""Store tests. No server needed -- these drive Store directly."""

from __future__ import annotations

import pytest

from client.state import AuctionView, BidView, ConnectionState, EventType, Store


def make_auction(**overrides) -> AuctionView:
    base = dict(
        auction_id="a1",
        item_name="Laptop",
        description="demo",
        starting_price=100.0,
        current_highest_bid=100.0,
        highest_bidder="",
        start_time=1000,
        end_time=2000,
        active=True,
        winner="",
    )
    base.update(overrides)
    return AuctionView(**base)


def make_bid(bid_id="b1", **overrides) -> BidView:
    base = dict(bid_id=bid_id, auction_id="a1", bidder="alice", amount=150.0, timestamp=1500)
    base.update(overrides)
    return BidView(**base)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def events(store: Store) -> list:
    captured: list = []
    store.subscribe(captured.append)
    return captured


def test_new_auction_emits_added(store, events):
    store.apply_auctions([make_auction()])
    assert [e.type for e in events] == [EventType.AUCTION_ADDED]


def test_unchanged_auction_emits_nothing(store, events):
    store.apply_auctions([make_auction()])
    events.clear()
    store.apply_auctions([make_auction()])
    assert events == []


def test_bid_increase_emits_update_with_changed_fields(store, events):
    store.apply_auctions([make_auction()])
    events.clear()
    store.apply_auctions([make_auction(current_highest_bid=150.0, highest_bidder="alice")])

    assert [e.type for e in events] == [EventType.AUCTION_UPDATED]
    assert set(events[0].payload["changed"]) == {"current_highest_bid", "highest_bidder"}


def test_closing_emits_closed_not_updated(store, events):
    store.apply_auctions([make_auction(current_highest_bid=150.0, highest_bidder="alice")])
    events.clear()
    store.apply_auctions(
        [make_auction(current_highest_bid=150.0, highest_bidder="alice", active=False, winner="alice")]
    )
    assert [e.type for e in events] == [EventType.AUCTION_CLOSED]


def test_outbid_emitted_only_for_current_user(store, events):
    store.set_session("alice", "token")
    store.apply_auctions([make_auction(current_highest_bid=150.0, highest_bidder="alice")])
    events.clear()

    store.apply_auctions([make_auction(current_highest_bid=200.0, highest_bidder="bob")])
    assert EventType.OUTBID in [e.type for e in events]


def test_no_outbid_when_someone_else_is_overtaken(store, events):
    store.set_session("alice", "token")
    store.apply_auctions([make_auction(current_highest_bid=150.0, highest_bidder="bob")])
    events.clear()

    store.apply_auctions([make_auction(current_highest_bid=200.0, highest_bidder="charlie")])
    assert EventType.OUTBID not in [e.type for e in events]


# These stand in for the version field the proto doesn't have -- without them
# an out-of-order poll reply rolls the UI backwards.


def test_lower_highest_bid_on_active_auction_is_rejected_as_stale(store, events):
    store.apply_auctions([make_auction(current_highest_bid=200.0, highest_bidder="bob")])
    events.clear()

    store.apply_auctions([make_auction(current_highest_bid=150.0, highest_bidder="alice")])

    assert events == []
    assert store.auction("a1").current_highest_bid == 200.0


def test_closed_auction_cannot_reopen(store, events):
    store.apply_auctions([make_auction(active=False, winner="alice")])
    events.clear()

    store.apply_auctions([make_auction(active=True)])

    assert events == []
    assert store.auction("a1").active is False


def test_bids_deduplicated_by_id(store, events):
    store.apply_bids("a1", [make_bid("b1")])
    events.clear()

    store.apply_bids("a1", [make_bid("b1"), make_bid("b2", amount=200.0)])

    assert [e.type for e in events] == [EventType.BID_PLACED]
    assert len(store.bids("a1")) == 2


def test_bids_sorted_by_amount_not_timestamp(store):
    # timestamp is whole seconds so concurrent bids collide; amount is a
    # total order because the server enforces increasing bids.
    store.apply_bids(
        "a1",
        [
            make_bid("b1", amount=100.0, timestamp=1500),
            make_bid("b2", amount=300.0, timestamp=1500),
            make_bid("b3", amount=200.0, timestamp=1500),
        ],
    )
    assert [b.amount for b in store.bids("a1")] == [300.0, 200.0, 100.0]


def test_logout_clears_auction_data(store):
    store.set_session("alice", "token")
    store.apply_auctions([make_auction()])
    store.clear_session()

    assert store.auctions() == []
    assert store.session.authenticated is False


def test_connection_change_is_deduplicated(store, events):
    store.set_connection(ConnectionState.CONNECTED)
    store.set_connection(ConnectionState.CONNECTED)
    assert len([e for e in events if e.type is EventType.CONNECTION_CHANGED]) == 1


def test_subscriber_exception_does_not_break_others(store):
    seen: list = []
    store.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    store.subscribe(seen.append)

    store.apply_auctions([make_auction()])
    assert len(seen) == 1


def test_auctions_sorted_active_first_then_soonest_ending(store):
    store.apply_auctions(
        [
            make_auction(auction_id="closed", active=False, end_time=1000),
            make_auction(auction_id="late", end_time=9000),
            make_auction(auction_id="soon", end_time=2000),
        ]
    )
    assert [a.auction_id for a in store.auctions()] == ["soon", "late", "closed"]
