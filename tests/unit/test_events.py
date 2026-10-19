import asyncio

import pytest

from smartrouter.telemetry.events import Event, EventBus, SubscriptionClosed


async def test_two_subscribers_receive_same_events_in_order() -> None:
    bus = EventBus()
    events = [Event("turn", {"n": 1}), Event("decision", {"n": 2}), Event("request", {"n": 3})]
    with bus.subscribe() as a, bus.subscribe() as b:
        for event in events:
            bus.publish(event)
        received_a = [await a.get() for _ in events]
        received_b = [await b.get() for _ in events]
    assert received_a == events
    assert received_b == events


async def test_closed_subscriber_stops_receiving() -> None:
    bus = EventBus()
    closed = bus.subscribe()
    keep = bus.subscribe()
    assert bus.subscriber_count() == 2

    closed.close()
    closed.close()
    assert bus.subscriber_count() == 1

    bus.publish(Event("turn", {"n": 1}))
    assert await keep.get() == Event("turn", {"n": 1})
    with pytest.raises(SubscriptionClosed):
        await asyncio.wait_for(closed.get(), timeout=0.5)
    keep.close()


async def test_close_wakes_a_waiting_iterator() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    received: list[Event] = []

    async def consume() -> None:
        async for event in sub:
            received.append(event)

    task = asyncio.create_task(consume())
    bus.publish(Event("request", {"n": 1}))
    await asyncio.sleep(0)
    sub.close()
    await asyncio.wait_for(task, timeout=0.5)
    assert received == [Event("request", {"n": 1})]


async def test_full_queue_drops_oldest_keeps_newest() -> None:
    bus = EventBus(max_queue=2)
    with bus.subscribe() as sub:
        for n in range(3):
            bus.publish(Event("turn", {"n": n}))
        assert await sub.get() == Event("turn", {"n": 1})
        assert await sub.get() == Event("turn", {"n": 2})


def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    bus.publish(Event("turn", {"n": 1}))
    assert bus.subscriber_count() == 0
