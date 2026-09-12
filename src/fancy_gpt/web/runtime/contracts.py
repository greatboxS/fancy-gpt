from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    supported_transports: tuple[str, ...]
    supported_browsers: tuple[str, ...]
    scopes: tuple[str, ...]
    automatic: bool = True
    existing_session: bool = False
    owns_browser_process: bool = False
    requires_extension: bool = False
    requires_browser_install: bool = False
    features: tuple[str, ...] = ("dom", "javascript", "persistent-auth")

    def supports_browser(self, browser: str) -> bool:
        return "any" in self.supported_browsers or browser in self.supported_browsers


class RuntimeRegistry:
    def __init__(self, contracts: list[RuntimeContract] | None = None) -> None:
        contracts = contracts or [
            RuntimeContract(
                id="extension",
                supported_transports=("native-messaging", "websocket"),
                supported_browsers=("chrome", "edge", "firefox"),
                scopes=("local", "remote"),
                existing_session=True,
                requires_extension=True,
            ),
            RuntimeContract(
                id="playwright",
                supported_transports=("local-process",),
                supported_browsers=("chromium", "firefox"),
                scopes=("local",),
                owns_browser_process=True,
                requires_browser_install=True,
            ),
            RuntimeContract(
                id="cdp",
                supported_transports=("cdp",),
                supported_browsers=("chrome", "edge"),
                scopes=("local", "remote"),
                existing_session=True,
            ),
            RuntimeContract(
                id="interactive",
                supported_transports=("human",),
                supported_browsers=("any", "chrome", "edge", "firefox", "safari"),
                scopes=("local", "remote", "any"),
                automatic=False,
                existing_session=True,
            ),
            RuntimeContract(
                id="fake",
                supported_transports=("in-memory",),
                supported_browsers=("fake",),
                scopes=("local",),
            ),
        ]
        self._items = {item.id: item for item in contracts}

    def get(self, runtime_id: str) -> RuntimeContract:
        try:
            return self._items[runtime_id]
        except KeyError as exc:
            raise KeyError(f"unknown browser runtime: {runtime_id}") from exc

    def all(self) -> list[RuntimeContract]:
        return sorted(self._items.values(), key=lambda item: item.id)
