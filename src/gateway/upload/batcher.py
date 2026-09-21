"""Accumulate items until there are N of them or the oldest is T seconds
old, whichever comes first.

Time is passed in, never read from the clock, so the tests are exact.
"""

from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar("T")


class Batcher(Generic[T]):
    def __init__(self, max_size: int, max_wait_seconds: float) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.max_size = max_size
        self.max_wait_seconds = max_wait_seconds
        self._items: list[T] = []
        self._oldest_at: datetime | None = None

    def __len__(self) -> int:
        return len(self._items)

    def add(self, item: T, now: datetime) -> list[T] | None:
        """Add one item. Returns the batch if this filled it, else None."""
        if self._oldest_at is None:
            self._oldest_at = now
        self._items.append(item)
        if len(self._items) >= self.max_size:
            return self.flush()
        return None

    def flush_if_due(self, now: datetime) -> list[T] | None:
        """Return the batch if the oldest item has waited long enough."""
        if self._oldest_at is None:
            return None
        if (now - self._oldest_at).total_seconds() >= self.max_wait_seconds:
            return self.flush()
        return None

    def flush(self) -> list[T]:
        items, self._items, self._oldest_at = self._items, [], None
        return items
