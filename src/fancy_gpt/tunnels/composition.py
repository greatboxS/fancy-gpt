from __future__ import annotations

from dataclasses import dataclass

from fancy_gpt.web.runtime import RuntimeRegistry
from fancy_gpt.web.sites import SiteRegistry
from fancy_gpt.web.transport import TransportRegistry

from .models import TunnelSpec


@dataclass(frozen=True)
class CompositionCheck:
    ok: bool
    errors: tuple[str, ...]


class TunnelCompositionValidator:
    """Validates a tunnel as a composition of independently testable layers."""

    def __init__(
        self,
        *,
        sites: SiteRegistry | None = None,
        runtimes: RuntimeRegistry | None = None,
        transports: TransportRegistry | None = None,
    ) -> None:
        self.sites = sites or SiteRegistry()
        self.runtimes = runtimes or RuntimeRegistry()
        self.transports = transports or TransportRegistry()

    def check(self, spec: TunnelSpec) -> CompositionCheck:
        errors: list[str] = []
        try:
            site = self.sites.get(spec.site)
        except KeyError as exc:
            errors.append(str(exc))
            site = None
        try:
            runtime = self.runtimes.get(spec.runtime.value)
        except KeyError as exc:
            errors.append(str(exc))
            runtime = None
        try:
            transport = self.transports.get(spec.transport.value)
        except KeyError as exc:
            errors.append(str(exc))
            transport = None

        if runtime is not None:
            if spec.transport.value not in runtime.supported_transports:
                errors.append(
                    f"runtime {runtime.id} does not support transport {spec.transport.value}"
                )
            if not runtime.supports_browser(spec.browser):
                errors.append(f"runtime {runtime.id} does not support browser {spec.browser}")
            if spec.scope.value not in runtime.scopes and "any" not in runtime.scopes:
                errors.append(f"runtime {runtime.id} does not support scope {spec.scope.value}")
            if runtime.requires_extension != spec.capabilities.requires_extension:
                errors.append(
                    f"runtime {runtime.id} requires_extension={runtime.requires_extension} conflicts with tunnel capability"
                )
            if runtime.requires_browser_install != spec.capabilities.requires_browser_install:
                errors.append(
                    f"runtime {runtime.id} requires_browser_install={runtime.requires_browser_install} conflicts with tunnel capability"
                )
            if site is not None:
                missing = sorted(set(site.required_browser_features) - set(runtime.features))
                if missing:
                    errors.append(f"runtime {runtime.id} lacks site-required features: {', '.join(missing)}")
            if runtime.automatic != spec.capabilities.automatic:
                errors.append(
                    f"runtime {runtime.id} automatic={runtime.automatic} conflicts with tunnel capability automatic={spec.capabilities.automatic}"
                )

        if transport is not None:
            if spec.scope.value not in transport.scopes and "any" not in transport.scopes:
                errors.append(f"transport {transport.id} does not support scope {spec.scope.value}")
            if spec.scope.value == "remote" and not transport.remote_capable:
                errors.append(f"transport {transport.id} is not remote capable")
            if spec.capabilities.requires_bridge != transport.requires_bridge:
                errors.append(
                    f"transport {transport.id} requires_bridge={transport.requires_bridge} conflicts with tunnel capability"
                )
            if spec.capabilities.remote_capable and not transport.remote_capable:
                errors.append(f"transport {transport.id} cannot satisfy remote_capable tunnel capability")
            if spec.capabilities.requires_native_host != transport.requires_native_host:
                errors.append(
                    f"transport {transport.id} requires_native_host={transport.requires_native_host} conflicts with tunnel capability"
                )
            if spec.capabilities.supports_ssh_forward and not transport.supports_ssh_forward:
                errors.append(f"transport {transport.id} does not support SSH forwarding")

        if spec.capabilities.browser_families and spec.browser not in spec.capabilities.browser_families and "any" not in spec.capabilities.browser_families:
            errors.append(f"tunnel browser {spec.browser} is absent from browser_families")

        if site is not None and spec.capabilities.supports_fresh_conversation and not site.supports_fresh_conversation:
            errors.append(f"site {site.id} does not support fresh conversations")

        return CompositionCheck(ok=not errors, errors=tuple(errors))

    def validate(self, spec: TunnelSpec) -> None:
        result = self.check(spec)
        if not result.ok:
            raise ValueError(f"invalid tunnel composition {spec.id}: " + "; ".join(result.errors))
