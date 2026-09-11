from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class ToolStats:
    calls: int = 0
    errors: int = 0
    total_duration_s: float = 0.0
    last_called_at: float | None = None
    last_error: str | None = None


class ServerStats:
    """Process-wide, in-memory counters for MCP tool invocations.

    Scoped to this server process; restarting the MCP server resets counts.
    Not persisted, since the goal is live visibility into "is this process
    handling requests", not a historical audit log (requests already persist
    per-request state under the work directory).
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._tools: dict[str, ToolStats] = {}

    def record(self, tool_name: str, *, duration_s: float, error: str | None) -> None:
        with self._lock:
            stats = self._tools.setdefault(tool_name, ToolStats())
            stats.calls += 1
            stats.total_duration_s += duration_s
            stats.last_called_at = time.time()
            if error is not None:
                stats.errors += 1
                stats.last_error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            tools = {
                name: {
                    "calls": stats.calls,
                    "errors": stats.errors,
                    "avg_duration_ms": round((stats.total_duration_s / stats.calls) * 1000, 1) if stats.calls else 0.0,
                    "last_called_at": stats.last_called_at,
                    "last_error": stats.last_error,
                }
                for name, stats in self._tools.items()
            }
            return {
                "started_at": self.started_at,
                "uptime_seconds": round(now - self.started_at, 1),
                "total_calls": sum(s.calls for s in self._tools.values()),
                "total_errors": sum(s.errors for s in self._tools.values()),
                "tools": tools,
            }


stats = ServerStats()


def tracked(tool_name: str) -> Callable[[F], F]:
    """Decorate an MCP tool function to record call count/latency/errors in `stats`."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            error: str | None = None
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                stats.record(tool_name, duration_s=time.monotonic() - started, error=error)

        return wrapper  # type: ignore[return-value]

    return decorator
