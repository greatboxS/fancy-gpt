from __future__ import annotations

import shutil
import subprocess
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MCPClientKind(str, Enum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    GEMINI = "gemini"


@dataclass(frozen=True)
class MCPClientStatus:
    client: MCPClientKind
    executable: str | None
    installed: bool
    linked: bool
    detail: str


class MCPClientRegistry:
    """Thin adapters for registering the FancyGPT stdio server with MCP clients.

    This layer intentionally owns only client-specific CLI syntax. It does not
    know anything about FancyGPT reasoning, projects, browser tunnels, or MCP
    tool implementation.
    """

    server_name = "fancy-gpt"

    def __init__(self, *, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    @staticmethod
    def _binary(kind: MCPClientKind) -> str:
        return {
            MCPClientKind.CODEX: "codex",
            MCPClientKind.CLAUDE_CODE: "claude",
            MCPClientKind.GEMINI: "gemini",
        }[kind]

    def executable(self, kind: MCPClientKind) -> str | None:
        return shutil.which(self._binary(kind))

    def _get_command(self, kind: MCPClientKind, executable: str) -> list[str]:
        if kind == MCPClientKind.GEMINI:
            return [executable, "mcp", "list"]
        return [executable, "mcp", "get", self.server_name]

    def _add_command(self, kind: MCPClientKind, executable: str, server_executable: str) -> list[str]:
        if kind == MCPClientKind.CLAUDE_CODE:
            return [
                executable,
                "mcp",
                "add",
                "--scope",
                "user",
                self.server_name,
                "--",
                server_executable,
            ]
        if kind == MCPClientKind.GEMINI:
            return [executable, "mcp", "add", "--scope", "user", self.server_name, server_executable]
        return [executable, "mcp", "add", self.server_name, "--", server_executable]

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=self.timeout_s,
        )

    def status(self, kind: MCPClientKind) -> MCPClientStatus:
        executable = self.executable(kind)
        if not executable:
            return MCPClientStatus(kind, None, False, False, "client executable not found")
        try:
            result = self._run(self._get_command(kind, executable))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return MCPClientStatus(kind, executable, True, False, f"status check failed: {type(exc).__name__}")
        linked = result.returncode == 0
        if kind == MCPClientKind.GEMINI:
            linked = linked and re.search(r"(?<![\w-])fancy-gpt(?![\w-])", result.stdout or "") is not None
        detail = (result.stdout or "").strip()
        return MCPClientStatus(kind, executable, True, linked, detail or ("linked" if linked else "not linked"))

    def register(self, kind: MCPClientKind, server_executable: str | Path) -> MCPClientStatus:
        server = str(Path(server_executable).expanduser().resolve())
        if not Path(server).exists():
            raise FileNotFoundError(server)
        before = self.status(kind)
        if not before.installed:
            return before
        if before.linked:
            return MCPClientStatus(kind, before.executable, True, True, "already linked")
        assert before.executable is not None
        try:
            result = self._run(self._add_command(kind, before.executable, server))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return MCPClientStatus(kind, before.executable, True, False, f"registration failed: {type(exc).__name__}")
        if result.returncode != 0:
            return MCPClientStatus(kind, before.executable, True, False, (result.stdout or "registration failed").strip())
        after = self.status(kind)
        if not after.linked:
            return MCPClientStatus(kind, before.executable, True, False, "registration command succeeded but verification failed")
        return MCPClientStatus(kind, before.executable, True, True, "linked")

    def register_detected(self, server_executable: str | Path) -> list[MCPClientStatus]:
        return [
            self.register(kind, server_executable)
            for kind in (MCPClientKind.CODEX, MCPClientKind.CLAUDE_CODE, MCPClientKind.GEMINI)
            if self.executable(kind)
        ]

    def all_status(self) -> list[MCPClientStatus]:
        return [self.status(kind) for kind in (MCPClientKind.CODEX, MCPClientKind.CLAUDE_CODE, MCPClientKind.GEMINI)]
