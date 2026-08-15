"""Generic polling adapters and a stoppable collection scheduler."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import Event, Thread
from time import monotonic
from typing import Union

from .adapters import TelemetryAdapter
from .core import ResourceMonitor
from .models import ResourceEvent, ResourceSnapshot

Record = Union[ResourceSnapshot, ResourceEvent]


class PollingAdapter:
    """Wrap any provider/node collector function as an RM adapter."""

    def __init__(self, source_id: str, collector: Callable[[], Iterable[Record]]) -> None:
        if not source_id:
            raise ValueError("source_id is required")
        self._source_id = source_id
        self._collector = collector

    @property
    def source_id(self) -> str:
        return self._source_id

    def collect(self) -> Iterable[Record]:
        return self._collector()


class CollectionScheduler:
    def __init__(self, monitor: ResourceMonitor, *, interval_seconds: float = 60.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.monitor = monitor
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self.last_run_monotonic: float | None = None
        self.last_error: str | None = None

    def run_once(self) -> int:
        try:
            count = self.monitor.collect_once()
            self.last_error = None
            self.last_run_monotonic = monotonic()
            return count
        except Exception as exc:
            self.last_error = str(exc)
            self.last_run_monotonic = monotonic()
            raise

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="resource-monitor-collector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # A failed adapter must not stop unrelated providers/nodes.
                pass
            self._stop.wait(self.interval_seconds)
