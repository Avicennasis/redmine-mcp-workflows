"""In-memory operational metrics.

Counts and latency for every tool call, aggregated per tool. Deliberately
dependency-free and process-local: an MCP server is short-lived and
single-operator, so a counter is enough to answer "what is used, what errors,
what is slow" without a metrics backend.
"""

from __future__ import annotations

import threading
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._errors: dict[str, int] = {}
        self._total_seconds: dict[str, float] = {}
        self._max_seconds: dict[str, float] = {}

    def record(self, name: str, seconds: float, *, error: bool = False) -> None:
        with self._lock:
            self._calls[name] = self._calls.get(name, 0) + 1
            if error:
                self._errors[name] = self._errors.get(name, 0) + 1
            self._total_seconds[name] = self._total_seconds.get(name, 0.0) + seconds
            if seconds > self._max_seconds.get(name, 0.0):
                self._max_seconds[name] = seconds

    def snapshot(self, *, reset: bool = False) -> dict[str, Any]:
        with self._lock:
            tools: dict[str, dict[str, Any]] = {}
            for name, calls in sorted(self._calls.items()):
                errors = self._errors.get(name, 0)
                total = self._total_seconds.get(name, 0.0)
                tools[name] = {
                    "calls": calls,
                    "errors": errors,
                    "total_seconds": round(total, 6),
                    "avg_ms": round((total / calls) * 1000, 3) if calls else 0.0,
                    "max_ms": round(self._max_seconds.get(name, 0.0) * 1000, 3),
                }
            snapshot = {
                "total_calls": sum(self._calls.values()),
                "total_errors": sum(self._errors.values()),
                "tools": tools,
            }
            if reset:
                self._calls.clear()
                self._errors.clear()
                self._total_seconds.clear()
                self._max_seconds.clear()
            return snapshot

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._errors.clear()
            self._total_seconds.clear()
            self._max_seconds.clear()


METRICS = Metrics()
