from __future__ import annotations

import subprocess
from pathlib import Path

from fancy_gpt.mcp_clients import MCPClientKind, MCPClientRegistry, MCPClientStatus


def _done(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=None)


def test_claude_registration_uses_user_scope_and_absolute_server(tmp_path: Path, monkeypatch) -> None:
    server = tmp_path / "fancy-gpt-mcp"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    registry = MCPClientRegistry()
    monkeypatch.setattr(registry, "executable", lambda kind: "/usr/bin/claude")
    calls: list[list[str]] = []
    results = iter([_done(1, "missing"), _done(0, "added"), _done(0, "fancy-gpt")])

    def run(command: list[str]):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(registry, "_run", run)
    status = registry.register(MCPClientKind.CLAUDE_CODE, server)
    assert status.linked
    assert calls[1] == [
        "/usr/bin/claude", "mcp", "add", "--scope", "user", "fancy-gpt", "--", str(server.resolve())
    ]


def test_codex_registration_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    server = tmp_path / "fancy-gpt-mcp"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    registry = MCPClientRegistry()
    monkeypatch.setattr(registry, "executable", lambda kind: "/usr/bin/codex")
    calls: list[list[str]] = []

    def run(command: list[str]):
        calls.append(command)
        return _done(0, "already configured")

    monkeypatch.setattr(registry, "_run", run)
    status = registry.register(MCPClientKind.CODEX, server)
    assert status.linked
    assert status.detail == "already linked"
    assert calls == [["/usr/bin/codex", "mcp", "get", "fancy-gpt"]]


def test_missing_client_is_reported_not_installed(monkeypatch) -> None:
    registry = MCPClientRegistry()
    monkeypatch.setattr(registry, "executable", lambda kind: None)
    status = registry.status(MCPClientKind.CLAUDE_CODE)
    assert status == MCPClientStatus(MCPClientKind.CLAUDE_CODE, None, False, False, "client executable not found")
