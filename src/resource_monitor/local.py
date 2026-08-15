"""Standard-library local CPU, memory, and disk telemetry."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from .models import ResourceSnapshot


CommandRunner = Callable[[list[str]], str]


def _run_command(command: list[str]) -> str:
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()


class LocalSystemCollector:
    """Collect one host snapshot without third-party runtime dependencies."""

    source_id = "local-system"

    def __init__(
        self,
        *,
        hostname: str | None = None,
        command_runner: CommandRunner = _run_command,
        disk_path: str = "/",
    ) -> None:
        self.hostname = hostname or socket.gethostname()
        self._run_command = command_runner
        self.disk_path = disk_path

    def collect(self) -> Iterable[ResourceSnapshot]:
        observed_at = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "authority": "observed",
            "health": "online",
            "availability": "available",
            "confidence": 1.0,
            "node_id": self.hostname,
            "host": self.hostname,
            "cpu": self._cpu(),
            "memory": self._memory(),
            "disk": self._disk(),
        }
        yield ResourceSnapshot(
            subject_id=self.hostname,
            subject_type="host",
            observed_at=observed_at,
            values=values,
            source_id=self.source_id,
        )

    def _cpu(self) -> dict[str, Any]:
        count = os.cpu_count() or 1
        try:
            load_1m = os.getloadavg()[0]
        except (AttributeError, OSError):
            load_1m = None
        return {
            "logical_processors": count,
            "load_1m": load_1m,
            "platform": platform.system().lower(),
        }

    def _memory(self) -> dict[str, Any]:
        if platform.system() == "Darwin":
            total = self._sysctl_bytes("hw.memsize")
            free = self._macos_free_memory()
        else:
            total, free = self._linux_memory()
        used = max(total - free, 0) if total is not None and free is not None else None
        return {"total_bytes": total, "free_bytes": free, "used_bytes": used}

    def _sysctl_bytes(self, key: str) -> int | None:
        try:
            return int(self._run_command(["sysctl", "-n", key]))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _macos_free_memory(self) -> int | None:
        try:
            output = self._run_command(["vm_stat"])
            page_size_match = re.search(r"page size of (\d+) bytes", output)
            page_size = int(page_size_match.group(1)) if page_size_match else 4096
            values = {}
            for line in output.splitlines():
                match = re.match(r"Pages (free|inactive|speculative):\s+(\d+)", line)
                if match:
                    values[match.group(1)] = int(match.group(2))
            return sum(values.values()) * page_size
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _linux_memory(self) -> tuple[int | None, int | None]:
        try:
            fields: dict[str, int] = {}
            for line in open("/proc/meminfo", encoding="utf-8"):
                name, value = line.split(":", 1)
                fields[name] = int(value.strip().split()[0]) * 1024
            total = fields.get("MemTotal")
            available = fields.get("MemAvailable", fields.get("MemFree"))
            return total, available
        except (OSError, ValueError):
            return None, None

    def _disk(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.disk_path)
        return {
            "path": self.disk_path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
