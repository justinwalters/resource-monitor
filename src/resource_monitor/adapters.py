"""Extension contracts for hosts, providers, endpoints, and other sources."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .models import ResourceEvent, ResourceSnapshot


class TelemetryAdapter(Protocol):
    """A source that can be installed without changing the RM core."""

    @property
    def source_id(self) -> str: ...

    def collect(self) -> Iterable[ResourceSnapshot | ResourceEvent]: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, TelemetryAdapter] = {}

    def register(self, adapter: TelemetryAdapter) -> None:
        if adapter.source_id in self._adapters:
            raise ValueError(f"adapter already registered: {adapter.source_id}")
        self._adapters[adapter.source_id] = adapter

    def collect_all(self) -> list[ResourceSnapshot | ResourceEvent]:
        records: list[ResourceSnapshot | ResourceEvent] = []
        for adapter in self._adapters.values():
            records.extend(adapter.collect())
        return records

