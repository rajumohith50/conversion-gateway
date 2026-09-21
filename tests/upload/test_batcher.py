from datetime import UTC, datetime, timedelta

import pytest

from gateway.upload.batcher import Batcher

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_fills_at_max_size() -> None:
    b: Batcher[str] = Batcher(max_size=3, max_wait_seconds=60)
    assert b.add("a", T0) is None
    assert b.add("b", T0) is None
    assert b.add("c", T0) == ["a", "b", "c"]
    assert len(b) == 0


def test_flushes_when_oldest_item_is_old_enough() -> None:
    b: Batcher[str] = Batcher(max_size=10, max_wait_seconds=5)
    b.add("a", T0)
    b.add("b", T0 + timedelta(seconds=4))
    assert b.flush_if_due(T0 + timedelta(seconds=4)) is None
    # Measured from the OLDEST item, not the newest.
    assert b.flush_if_due(T0 + timedelta(seconds=5)) == ["a", "b"]
    assert b.flush_if_due(T0 + timedelta(seconds=100)) is None  # now empty


def test_whichever_first() -> None:
    b: Batcher[int] = Batcher(max_size=2, max_wait_seconds=5)
    b.add(1, T0)
    assert b.flush_if_due(T0 + timedelta(seconds=1)) is None
    assert b.add(2, T0 + timedelta(seconds=1)) == [1, 2]  # size won


def test_zero_wait_flushes_immediately() -> None:
    b: Batcher[int] = Batcher(max_size=100, max_wait_seconds=0)
    b.add(1, T0)
    assert b.flush_if_due(T0) == [1]


def test_manual_flush_and_validation() -> None:
    b: Batcher[int] = Batcher(max_size=5, max_wait_seconds=5)
    assert b.flush() == []
    b.add(1, T0)
    assert b.flush() == [1]
    with pytest.raises(ValueError):
        Batcher(max_size=0, max_wait_seconds=1)
