from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fancy_gpt.models import AutomatedModelResponse, InteractionRequired, ModelRequest


class InteractiveModelProvider(Protocol):
    name: str

    def prepare(
        self,
        request: ModelRequest,
        prompt_file: Path,
        open_browser: bool = False,
    ) -> InteractionRequired:
        ...


class AutomaticModelProvider(Protocol):
    name: str

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def execute(self, request: ModelRequest) -> AutomatedModelResponse:
        ...
