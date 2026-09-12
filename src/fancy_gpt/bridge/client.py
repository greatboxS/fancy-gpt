from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from websockets.sync.client import ClientConnection, connect

from fancy_gpt.browser.base import BrowserResponse, BrowserTurn

from .probe import fetch_job_progress
from .protocol import PROTOCOL_VERSION, dumps, hello, loads


@dataclass
class _PendingTurn:
    prompt: str | None = None
    conversation_id: str | None = None
    conversation_mode: str = "temporary"


class BridgeBrowserDriver:
    """BrowserDriver implemented through a FancyGPT bridge server.

    The browser may be on the same host (extension + Native Messaging) or on a
    different workstation (extension + WebSocket carried through SSH). Core
    code sees both as the same tunnel runtime.
    """

    name = "extension-bridge"

    def __init__(self, endpoint: str, token: str, tunnel_id: str, *, job_timeout_s: float = 300.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.tunnel_id = tunnel_id
        self.job_timeout_s = job_timeout_s
        self._connection: ClientConnection | None = None
        self._turns: dict[str, _PendingTurn] = {}

    def start(self) -> None:
        if self._connection is not None:
            return
        connection = connect(self.endpoint, open_timeout=10, max_size=8 * 1024 * 1024)
        connection.send(dumps(hello(role="controller", token=self.token)))
        ack = loads(connection.recv(timeout=10))
        if ack.get("type") != "hello_ack":
            connection.close()
            raise RuntimeError(f"bridge refused controller: {ack}")
        self._connection = connection

    def stop(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._turns.clear()

    def _conn(self) -> ClientConnection:
        if self._connection is None:
            raise RuntimeError("bridge driver is not started")
        return self._connection

    def health_check(self) -> None:
        connection = self._conn()
        started = time.monotonic()
        connection.send(dumps({"type": "probe", "tunnel_id": self.tunnel_id}))
        result = loads(connection.recv(timeout=10))
        if result.get("type") != "probe_result" or not result.get("available"):
            raise RuntimeError(f"browser bridge unavailable for {self.tunnel_id}")
        _ = time.monotonic() - started

    def site_health(self, *, timeout_s: float = 20.0) -> dict:
        """Probe the actual site adapter through the registered browser worker."""
        job_id = f"health-{uuid.uuid4().hex}"
        self._conn().send(dumps({
            "type": "job",
            "job_id": job_id,
            "tunnel_id": self.tunnel_id,
            "site": "chatgpt",
            "operation": "site.health",
            "request_id": job_id,
            "stage": "health",
            "conversation": {"mode": "fresh"},
            "timeout_s": min(timeout_s, self.job_timeout_s),
        }))
        result = loads(self._conn().recv(timeout=timeout_s))
        if result.get("job_id") != job_id:
            raise RuntimeError("bridge site-health response identity mismatch")
        if result.get("type") == "job_error":
            raise RuntimeError(str(result.get("error", "site health failed")))
        if result.get("type") != "job_result":
            raise RuntimeError(f"unexpected site-health response: {result.get('type')}")
        import json
        payload = json.loads(str(result.get("text") or "{}"))
        if not isinstance(payload, dict):
            raise RuntimeError("site health response is malformed")
        if not payload.get("ok"):
            raise RuntimeError(f"site not ready: {payload.get('reason', 'unknown')}")
        return payload

    def begin_turn(
        self,
        *,
        request_id: str,
        stage: str,
        conversation_id: str | None = None,
        conversation_mode: str = "temporary",
    ) -> BrowserTurn:
        turn_id = f"bridge-{uuid.uuid4().hex}"
        self._turns[turn_id] = _PendingTurn(conversation_id=conversation_id, conversation_mode=conversation_mode)
        return BrowserTurn(
            turn_id=turn_id,
            request_id=request_id,
            stage=stage,
            conversation_id=conversation_id,
            conversation_mode=conversation_mode,
        )

    def submit(self, turn: BrowserTurn, prompt: str) -> None:
        pending = self._turns.get(turn.turn_id)
        if pending is None:
            raise RuntimeError("unknown bridge turn")
        if pending.prompt is not None:
            raise RuntimeError("bridge turn already submitted")
        pending.prompt = prompt
        if pending.conversation_id:
            conversation = {"mode": "continue", "conversation_id": pending.conversation_id}
        elif pending.conversation_mode == "persistent":
            conversation = {"mode": "persistent"}
        else:
            conversation = {"mode": "fresh"}
        self._conn().send(dumps({
            "type": "job",
            "job_id": turn.turn_id,
            "tunnel_id": self.tunnel_id,
            "site": "chatgpt",
            "operation": "model.turn",
            "request_id": turn.request_id,
            "stage": turn.stage,
            "prompt": prompt,
            "conversation": conversation,
            "timeout_s": self.job_timeout_s,
        }))

    def wait_for_response(self, turn: BrowserTurn, *, timeout_s: float) -> BrowserResponse:
        if turn.turn_id not in self._turns:
            raise RuntimeError("unknown bridge turn")
        result = loads(self._conn().recv(timeout=timeout_s))
        if result.get("job_id") != turn.turn_id:
            raise RuntimeError("bridge response job identity mismatch")
        if result.get("type") == "job_error":
            raise RuntimeError(str(result.get("error", "browser bridge job failed")))
        if result.get("type") != "job_result":
            raise RuntimeError(f"unexpected bridge response: {result.get('type')}")
        text = str(result.get("text", ""))
        if not text:
            raise RuntimeError("bridge response is empty")
        conversation_id = result.get("conversation_id")
        return BrowserResponse(
            turn_id=turn.turn_id,
            text=text,
            response_identity=str(result.get("response_identity") or result.get("assistant_turn_id") or turn.turn_id),
            conversation_id=str(conversation_id) if conversation_id else None,
        )

    def close_turn(self, turn: BrowserTurn) -> None:
        self._turns.pop(turn.turn_id, None)

    def poll_progress(self, turn_id: str) -> str | None:
        """Best-effort snapshot of the in-flight response text for `turn_id`.

        Opens its own short-lived connection so it never contends with the
        primary controller connection blocked in wait_for_response().
        Returns None on any failure (bridge busy, no progress yet, etc.);
        this is a monitoring aid, never allowed to affect the actual turn.
        """
        try:
            progress = fetch_job_progress(self.endpoint, self.token, turn_id, open_timeout_s=1.0)
        except Exception:
            return None
        return progress.get("text") if progress else None
