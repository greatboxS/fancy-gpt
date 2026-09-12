from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LayerKind(str, Enum):
    SITE = "site"
    RUNTIME = "runtime"
    TRANSPORT = "transport"
    COMPOSITION = "composition"


class LayerHealth(StrictModel):
    layer: LayerKind
    component: str
    state: str
    detail: str
    metadata: dict[str, Any] = Field(default_factory=dict)
