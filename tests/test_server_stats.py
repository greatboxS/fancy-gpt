from __future__ import annotations

import pytest

from fancy_gpt.server_stats import ServerStats


def test_snapshot_counts_calls_and_errors(monkeypatch):
    import fancy_gpt.server_stats as module

    fresh = ServerStats()
    monkeypatch.setattr(module, "stats", fresh)

    @module.tracked("ok_tool")
    def ok() -> str:
        return "done"

    @module.tracked("bad_tool")
    def bad() -> str:
        raise ValueError("boom")

    ok()
    ok()
    with pytest.raises(ValueError):
        bad()

    snapshot = fresh.snapshot()
    assert snapshot["total_calls"] == 3
    assert snapshot["total_errors"] == 1
    assert snapshot["tools"]["ok_tool"]["calls"] == 2
    assert snapshot["tools"]["ok_tool"]["errors"] == 0
    assert snapshot["tools"]["bad_tool"]["calls"] == 1
    assert snapshot["tools"]["bad_tool"]["errors"] == 1
    assert "boom" in snapshot["tools"]["bad_tool"]["last_error"]
    assert snapshot["uptime_seconds"] >= 0
