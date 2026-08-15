"""Configuration-driven adapter construction without vendor assumptions."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters import TelemetryAdapter
from .collection import CollectionScheduler
from .core import ResourceMonitor

AdapterFactory = Callable[[dict[str, Any]], TelemetryAdapter]


class AdapterFactoryRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, adapter_type: str, factory: AdapterFactory) -> None:
        if not adapter_type or adapter_type in self._factories:
            raise ValueError("adapter type must be non-empty and unique")
        self._factories[adapter_type] = factory

    def build(self, config: dict[str, Any]) -> list[TelemetryAdapter]:
        sources = config.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("sources must be a list")
        adapters = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("each source must be an object")
            adapter_type = source.get("type")
            if adapter_type not in self._factories:
                raise ValueError(f"no adapter factory registered for: {adapter_type}")
            adapters.append(self._factories[adapter_type](source))
        return adapters


def load_config(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("configuration root must be an object")
    return payload


def configure_monitor(monitor: ResourceMonitor, config: dict[str, Any], factories: AdapterFactoryRegistry) -> CollectionScheduler:
    for adapter in factories.build(config):
        monitor.add_adapter(adapter)
    interval = config.get("collection_interval_seconds", 60.0)
    return CollectionScheduler(monitor, interval_seconds=float(interval))

