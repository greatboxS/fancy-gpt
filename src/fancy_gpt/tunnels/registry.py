from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import yaml

from .composition import TunnelCompositionValidator
from .models import TunnelSpec


class TunnelRegistry:
    def __init__(
        self,
        specs: list[TunnelSpec] | None = None,
        *,
        validator: TunnelCompositionValidator | None = None,
        extra_files: list[Path] | None = None,
    ) -> None:
        self.validator = validator or TunnelCompositionValidator()
        items = list(specs) if specs is not None else self._load_builtin()
        if specs is None:
            configured = os.getenv("FANCY_GPT_TUNNELS_FILE", "").strip()
            paths = list(extra_files or [])
            if configured:
                paths.extend(Path(item) for item in configured.split(os.pathsep) if item)
            for path in paths:
                items.extend(self._load_file(path))
        for item in items:
            self.validator.validate(item)
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tunnel id in registry; custom catalogs may add but not silently override tunnels")
        self._specs = {item.id: item for item in items}

    @staticmethod
    def _parse_payload(payload: object, source: str) -> list[TunnelSpec]:
        if not isinstance(payload, dict):
            raise ValueError(f"tunnel catalog {source} must be a mapping")
        tunnels = payload.get("tunnels", [])
        if not isinstance(tunnels, list):
            raise ValueError(f"tunnel catalog {source} tunnels must be a list")
        return [TunnelSpec.model_validate(item) for item in tunnels]

    @classmethod
    def _load_builtin(cls) -> list[TunnelSpec]:
        resource = files("fancy_gpt").joinpath("catalog/tunnels.yaml")
        return cls._parse_payload(yaml.safe_load(resource.read_text(encoding="utf-8")) or {}, "builtin")

    @classmethod
    def _load_file(cls, path: Path) -> list[TunnelSpec]:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"tunnel catalog not found: {resolved}")
        return cls._parse_payload(yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}, str(resolved))

    def all(self) -> list[TunnelSpec]:
        return sorted(self._specs.values(), key=lambda item: (item.priority, item.id))

    def get(self, tunnel_id: str) -> TunnelSpec:
        try:
            return self._specs[tunnel_id]
        except KeyError as exc:
            raise KeyError(f"unknown tunnel: {tunnel_id}") from exc

    def effective(self) -> list[TunnelSpec]:
        disabled = {
            item.strip()
            for item in os.getenv("FANCY_GPT_DISABLED_TUNNELS", "").split(",")
            if item.strip()
        }
        return [item for item in self.all() if item.enabled and item.id not in disabled]

    def is_effective(self, tunnel_id: str) -> bool:
        return any(item.id == tunnel_id for item in self.effective())
