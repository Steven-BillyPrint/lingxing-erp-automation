"""Bound CPU-heavy queue reads per database without retaining idle resources.

SQLite invokes Python business-policy functions while scanning a page. Parallel
scans in the same process contend for the GIL, particularly under a container CPU
quota. FIFO admission keeps these scans predictable; ordinary writes and other
databases do not participate. Acquire before opening the read connection.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator
from weakref import WeakValueDictionary


class _ReadSlot:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.waiters: deque[object] = deque()

    @contextmanager
    def enter(self) -> Iterator[None]:
        ticket = object()
        with self.condition:
            self.waiters.append(ticket)
            try:
                while self.waiters[0] is not ticket:
                    self.condition.wait()
            except BaseException:
                self.waiters.remove(ticket)
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            with self.condition:
                self.waiters.popleft()
                self.condition.notify_all()


_registry_lock = threading.Lock()
_slots: WeakValueDictionary[str, _ReadSlot] = WeakValueDictionary()


@contextmanager
def queue_read_slot(path: str | Path) -> Iterator[None]:
    key = os.path.normcase(str(Path(path).resolve()))
    with _registry_lock:
        slot = _slots.get(key)
        if slot is None:
            slot = _ReadSlot()
            _slots[key] = slot
    # The context owns the strong reference, including while waiting. Once the
    # last request exits, the weak registry drops this database automatically.
    with slot.enter():
        yield
