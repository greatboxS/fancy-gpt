#!/usr/bin/env python3
"""Drive a real Codex binary through FancyGPT's Responses API and one local tool call."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from fancy_gpt.gateway import GatewayService
from fancy_gpt.gateway_app import create_app
from fancy_gpt.gateway_clients import codex_command
from fancy_gpt.models import AutomatedModelResponse
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class SmokeProvider:
    name = "codex-smoke-web"

    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def execute(self, request, *, on_progress=None):
        self.calls += 1
        if self.calls == 1:
            envelope = {
                "type": "tool_calls",
                "calls": [{
                    "id": "smoke_exec",
                    "name": "exec_command",
                    "arguments": {"cmd": "printf FANCY_CODEX_TOOL_OK"},
                }],
            }
        else:
            if "TOOL RESULT smoke_exec" not in request.prompt or "FANCY_CODEX_TOOL_OK" not in request.prompt:
                raise RuntimeError("Codex tool result did not return to the model gateway")
            envelope = {"type": "message", "text": "FANCY_CODEX_REMOTE_OK"}
            if on_progress is not None:
                on_progress(json.dumps(envelope))
        return AutomatedModelResponse(
            request_id=request.request_id,
            stage="agent",
            provider=self.name,
            raw_text=json.dumps(envelope),
            response_identity=f"smoke-response-{self.calls}",
            conversation_id="smoke-conversation",
        )


class SmokeManager:
    def __init__(self, provider: SmokeProvider) -> None:
        self.model = provider

    def select(self, **_kwargs) -> TunnelSelection:
        return TunnelSelection(
            tunnel_id="smoke-browser",
            reason="real Codex protocol smoke",
            explicit=True,
            health=TunnelHealth(
                tunnel_id="smoke-browser",
                state=TunnelHealthState.HEALTHY,
                detail="in-process fake browser model",
            ),
        )

    def provider(self, _selection: TunnelSelection) -> SmokeProvider:
        return self.model


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    codex = shutil.which("codex")
    if codex is None:
        raise SystemExit("codex executable not found")

    state_root = root / ".fancy-gpt"
    state_root.mkdir(exist_ok=True)
    provider = SmokeProvider()
    with tempfile.TemporaryDirectory(prefix="codex-gateway-smoke-", dir=state_root) as temp:
        temp_root = Path(temp)
        service = GatewayService(temp_root / "gateway", manager=SmokeManager(provider))
        app = create_app(service)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("smoke gateway did not start")

        base_url = f"http://127.0.0.1:{port}"
        command = codex_command(
            [
                "--ask-for-approval", "never",
                "--sandbox", "danger-full-access",
                "exec",
                "Execute the requested tool when needed, then finish.",
            ],
            base_url=base_url,
            executable=codex,
        )
        codex_home = temp_root / "codex-home"
        codex_home.mkdir()
        env = {**dict(__import__("os").environ), "CODEX_HOME": str(codex_home)}
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        server.should_exit = True
        thread.join(timeout=5)
        output = completed.stdout + completed.stderr
        ok = completed.returncode == 0 and "FANCY_CODEX_REMOTE_OK" in output and provider.calls == 2
        print(json.dumps({
            "ok": ok,
            "codex": codex,
            "codex_exit": completed.returncode,
            "provider_turns": provider.calls,
            "tool_result_seen": "FANCY_CODEX_TOOL_OK" in output,
            "final_seen": "FANCY_CODEX_REMOTE_OK" in output,
        }, indent=2))
        if not ok:
            print(output[-8000:])
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
