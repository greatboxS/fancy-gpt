from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from websockets.sync.client import ClientConnection, connect

from fancy_gpt.browser.base import BrowserResponse, BrowserTurn

from .probe import fetch_job_progress, send_job_cancel


class SiteHealthUnsupported(RuntimeError):
    """The connected browser worker predates the site-health probe.

    Raised instead of a plain error so callers can tell "this worker cannot be
    asked" apart from "this worker says the site is not ready".
    """

from .progress import ProgressSubscription
from .protocol import PROTOCOL_VERSION, dumps, hello, loads


@dataclass
class _PendingTurn:
    prompt: str | None = None
    conversation_id: str | None = None
    conversation_mode: str = "temporary"
    site: str | None = None


class BrowserTurnCancelled(RuntimeError):
    """Generation was stopped in the browser before it finished.

    Carries whatever partial text existed, and whether the stop control was
    actually clicked, so the caller can tell a genuinely stopped turn from one
    that had already finished when the cancel arrived.
    """

    def __init__(
        self,
        *,
        reason: str,
        partial_text: str = "",
        stopped_generation: bool = False,
        conversation_id: str | None = None,
    ) -> None:
        super().__init__(f"browser turn cancelled: {reason}")
        self.reason = reason
        self.partial_text = partial_text
        self.stopped_generation = stopped_generation
        self.conversation_id = conversation_id


class BridgeBrowserDriver:
    """BrowserDriver implemented through a FancyGPT bridge server.

    The browser may be on the same host (extension + Native Messaging) or on a
    different workstation (extension + WebSocket carried through SSH). Core
    code sees both as the same tunnel runtime.
    """

    name = "extension-bridge"

    def __init__(
        self,
        endpoint: str,
        token: str,
        tunnel_id: str,
        *,
        # Backstop only; the adapter stalls a turn on inactivity long before
        # this, and a legitimate long reasoning turn must not be cut off.
        job_timeout_s: float = 1800.0,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.tunnel_id = tunnel_id
        self.job_timeout_s = job_timeout_s
        self._connection: ClientConnection | None = None
        self._turns: dict[str, _PendingTurn] = {}
        self._progress: ProgressSubscription | None = None

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
        if self._progress is not None:
            self._progress.close()
            self._progress = None
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

    def site_health(self, site: str, *, timeout_s: float = 20.0) -> dict:
        """Probe the actual site adapter through the registered browser worker."""
        job_id = f"health-{uuid.uuid4().hex}"
        self._conn().send(dumps({
            "type": "job",
            "job_id": job_id,
            "tunnel_id": self.tunnel_id,
            "site": site,
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
            error = str(result.get("error", "site health failed"))
            if "unsupported operation" in error:
                raise SiteHealthUnsupported(
                    "the connected browser extension is older than this FancyGPT build; "
                    "re-export it with 'fancy-gpt extension export' and reload it in the browser"
                )
            raise RuntimeError(error)
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
        site: str | None = None,
    ) -> BrowserTurn:
        turn_id = f"bridge-{uuid.uuid4().hex}"
        self._turns[turn_id] = _PendingTurn(
            conversation_id=conversation_id, conversation_mode=conversation_mode, site=site
        )
        return BrowserTurn(
            turn_id=turn_id,
            request_id=request_id,
            stage=stage,
            conversation_id=conversation_id,
            conversation_mode=conversation_mode,
            site=site,
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
            "site": pending.site,
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
        if result.get("type") == "job_cancelled":
            raise BrowserTurnCancelled(
                reason=str(result.get("reason") or "cancelled"),
                partial_text=str(result.get("text") or ""),
                stopped_generation=bool(result.get("stopped_generation")),
                conversation_id=(str(result["conversation_id"]) if result.get("conversation_id") else None),
            )
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

    def watch_progress(self, turn_id: str, on_progress) -> bool:
        """Have the bridge push this turn's progress instead of polling for it.

        Returns False when the push feed is unavailable, so the caller can fall
        back to polling rather than silently losing streaming.
        """
        if self._progress is None:
            self._progress = ProgressSubscription(self.endpoint, self.token, self.tunnel_id)
        return self._progress.subscribe(turn_id, on_progress)

    def stop_watching_progress(self, turn_id: str) -> None:
        """Release the route at the terminal reply, so a snapshot that arrives
        after completion has nowhere to go."""
        if self._progress is not None:
            self._progress.unsubscribe(turn_id)

    def cancel_turn(self, turn_id: str, *, generation_epoch: int = 0, reason: str = "cancelled") -> bool:
        """Ask the browser to stop generating this turn.

        Sent on its own short-lived connection, because the primary controller
        connection is blocked in wait_for_response() for this very turn. The
        turn's reply still arrives there: stopping generation makes the content
        script finish, which produces the normal job_result or job_error.
        """
        try:
            return send_job_cancel(
                self.endpoint, self.token, self.tunnel_id, turn_id,
                generation_epoch=generation_epoch, reason=reason,
            )
        except Exception:
            return False

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
