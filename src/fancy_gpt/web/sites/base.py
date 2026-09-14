from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SiteContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    hosts: tuple[str, ...]
    supports_fresh_conversation: bool = True
    operations: tuple[str, ...] = ("model.turn",)
    required_browser_features: tuple[str, ...] = ("dom", "javascript")
    metadata: dict[str, str] = Field(default_factory=dict)

    def accepts_host(self, host: str) -> bool:
        return host.lower() in {item.lower() for item in self.hosts}


class SiteRegistry:
    def __init__(self, contracts: list[SiteContract] | None = None) -> None:
        if contracts is None:
            from .chatgpt import CHATGPT_SITE
            from .gemini import GEMINI_SITE
            from .grok import GROK_SITE
            from .kimi import KIMI_SITE
            from .glm import GLM_SITE
            contracts = [CHATGPT_SITE, GEMINI_SITE, GROK_SITE, KIMI_SITE, GLM_SITE]
        self._items = {item.id: item for item in contracts}

    def get(self, site_id: str) -> SiteContract:
        try:
            return self._items[site_id]
        except KeyError as exc:
            raise KeyError(f"unknown site adapter: {site_id}") from exc

    def all(self) -> list[SiteContract]:
        return sorted(self._items.values(), key=lambda item: item.id)
