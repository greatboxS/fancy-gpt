from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fancy_gpt.web.models import LayerHealth


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TunnelRuntime(str, Enum):
    EXTENSION = "extension"
    PLAYWRIGHT = "playwright"
    CDP = "cdp"
    INTERACTIVE = "interactive"
    FAKE = "fake"


class TunnelTransport(str, Enum):
    NATIVE_MESSAGING = "native-messaging"
    WEBSOCKET = "websocket"
    LOCAL_PROCESS = "local-process"
    CDP = "cdp"
    HUMAN = "human"
    IN_MEMORY = "in-memory"


class TunnelScope(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    ANY = "any"


class TunnelHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TunnelCapabilities(StrictModel):
    automatic: bool = True
    remote_capable: bool = False
    existing_session: bool = False
    persistent_session: bool = True
    headless: bool = False
    requires_extension: bool = False
    requires_browser_install: bool = False
    requires_bridge: bool = False
    requires_native_host: bool = False
    supports_ssh_forward: bool = False
    supports_fresh_conversation: bool = True
    browser_families: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=lambda: ["linux", "darwin", "win32"])


class TunnelSpec(StrictModel):
    id: str
    description: str
    runtime: TunnelRuntime
    transport: TunnelTransport
    scope: TunnelScope = TunnelScope.LOCAL
    browser: str
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True
    endpoint: str | None = None
    capabilities: TunnelCapabilities = Field(default_factory=TunnelCapabilities)
    config: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class TunnelHealth(StrictModel):
    tunnel_id: str
    state: TunnelHealthState
    detail: str
    latency_ms: float | None = None
    browser_connected: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    layers: list[LayerHealth] = Field(default_factory=list)


class TunnelSelection(StrictModel):
    tunnel_id: str
    reason: str
    explicit: bool
    health: TunnelHealth
    alternatives: list[str] = Field(default_factory=list)


TunnelPolicy = Literal[
    "auto",
    "prefer-extension",
    "prefer-native",
    "prefer-remote",
    "prefer-playwright",
    "prefer-cdp",
]
