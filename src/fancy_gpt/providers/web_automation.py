from __future__ import annotations

from fancy_gpt.browser import BrowserDriver, BrowserPromptTooLargeError
from fancy_gpt.models import AutomatedModelResponse, ModelRequest


class ChatGPTWebAutomationProvider:
    """Automatic ChatGPT Web provider behind a replaceable BrowserDriver.

    The provider owns orchestration only. Authentication/session persistence and
    all UI-specific behavior belong to the driver. This separation allows full
    unit/integration testing with FakeBrowserDriver without network access.
    """

    name = "chatgpt-web-automation"

    def __init__(
        self,
        driver: BrowserDriver,
        *,
        timeout_s: float = 300.0,
        max_prompt_chars: int = 300_000,
        tunnel_id: str | None = None,
    ) -> None:
        self.driver = driver
        self.timeout_s = timeout_s
        self.max_prompt_chars = max_prompt_chars
        self.tunnel_id = tunnel_id
        if tunnel_id:
            self.name = f"chatgpt-web-automation:{tunnel_id}"
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.driver.start()
            try:
                self.driver.health_check()
                site_health = getattr(self.driver, "site_health", None)
                if callable(site_health):
                    site_health(timeout_s=min(20.0, self.timeout_s))
            except Exception:
                self.driver.stop()
                raise
            self._started = True

    def stop(self) -> None:
        if self._started:
            self.driver.stop()
            self._started = False

    def execute(self, request: ModelRequest) -> AutomatedModelResponse:
        if not self._started:
            raise RuntimeError("automation provider is not started")
        if len(request.prompt) > self.max_prompt_chars:
            raise BrowserPromptTooLargeError(
                f"compiled prompt has {len(request.prompt)} characters; "
                f"configured browser limit is {self.max_prompt_chars}"
            )
        conversation = {
            "mode": request.metadata.get("conversation_strategy", "fresh"),
            "binding": request.metadata.get("conversation_binding"),
        }
        begin_managed = getattr(self.driver, "begin_managed_turn", None)
        if callable(begin_managed):
            turn = begin_managed(
                request_id=request.request_id,
                stage=request.stage,
                conversation=conversation,
            )
        else:
            if conversation["mode"] != "fresh":
                raise RuntimeError(f"browser driver {self.driver.name} does not support conversation strategy {conversation['mode']}")
            turn = self.driver.begin_turn(request_id=request.request_id, stage=request.stage)
        try:
            self.driver.submit(turn, request.prompt)
            response = self.driver.wait_for_response(turn, timeout_s=self.timeout_s)
            if response.turn_id != turn.turn_id:
                raise RuntimeError("browser response is not bound to the submitted turn")
            return AutomatedModelResponse(
                request_id=request.request_id,
                stage=request.stage,
                provider=self.name,
                raw_text=response.text,
                response_identity=response.response_identity,
                conversation_binding=response.conversation_binding,
            )
        finally:
            self.driver.close_turn(turn)

    def __enter__(self) -> "ChatGPTWebAutomationProvider":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()
