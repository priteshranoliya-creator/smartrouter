"""In-process event bus for telemetry events (publish is sync, delivery is per-subscriber queue)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any


class SubscriptionClosed(Exception):
    """Raised by Subscription.get() once the subscription is closed."""


@dataclass(frozen=True)
class Event:
    """A telemetry event: a type tag such as "turn" plus a payload."""

    type: str
    data: dict[str, Any]


class Subscription:
    """One subscriber's bounded queue of events, closed via close() or a `with` block."""

    def __init__(self, max_queue: int, unregister: Callable[[Subscription], None]) -> None:
        # None is the close sentinel, so close() wakes a reader already waiting on an empty queue.
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=max_queue)
        self._unregister = unregister
        self._closed = False

    def _put(self, item: Event | None) -> None:
        # Never blocks: a full queue sheds its oldest item to make room for the newest.
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(item)

    def _deliver(self, event: Event) -> None:
        if not self._closed:
            self._put(event)

    async def get(self) -> Event:
        if self._closed and self._queue.empty():
            raise SubscriptionClosed
        item = await self._queue.get()
        if item is None:
            raise SubscriptionClosed
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unregister(self)
        self._put(None)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        try:
            return await self.get()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    def __enter__(self) -> Subscription:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class EventBus:
    """Fan-out of published events to every active subscription."""

    def __init__(self, max_queue: int = 1000) -> None:
        self._max_queue = max_queue
        self._subscribers: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        sub = Subscription(self._max_queue, self._subscribers.discard)
        self._subscribers.add(sub)
        return sub

    def publish(self, event: Event) -> None:
        for sub in list(self._subscribers):
            sub._deliver(event)

    def subscriber_count(self) -> int:
        return len(self._subscribers)
