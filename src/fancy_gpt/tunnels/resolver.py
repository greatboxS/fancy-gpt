from __future__ import annotations

from collections.abc import Callable

from .models import TunnelHealth, TunnelHealthState, TunnelPolicy, TunnelSelection, TunnelSpec
from .registry import TunnelRegistry

HealthProbe = Callable[[TunnelSpec], TunnelHealth]


class TunnelResolver:
    def __init__(self, registry: TunnelRegistry, probe: HealthProbe) -> None:
        self.registry = registry
        self.probe = probe

    @staticmethod
    def _policy_bonus(spec: TunnelSpec, policy: TunnelPolicy) -> int:
        if policy == "prefer-extension" and spec.runtime.value == "extension":
            return -100
        if policy == "prefer-native" and spec.transport.value == "native-messaging":
            return -100
        if policy == "prefer-remote" and spec.scope.value == "remote":
            return -100
        if policy == "prefer-playwright" and spec.runtime.value == "playwright":
            return -100
        if policy == "prefer-cdp" and spec.runtime.value == "cdp":
            return -100
        return 0

    def select(
        self,
        *,
        tunnel_id: str | None = None,
        policy: TunnelPolicy = "auto",
        require_automatic: bool = True,
    ) -> TunnelSelection:
        if tunnel_id:
            spec = self.registry.get(tunnel_id)
            if not self.registry.is_effective(tunnel_id):
                raise RuntimeError(f"tunnel {tunnel_id} is disabled")
            if require_automatic and not spec.capabilities.automatic:
                raise RuntimeError(f"tunnel {tunnel_id} is not automatic")
            health = self.probe(spec)
            if health.state == TunnelHealthState.UNAVAILABLE:
                raise RuntimeError(f"tunnel {tunnel_id} unavailable: {health.detail}")
            return TunnelSelection(
                tunnel_id=spec.id,
                reason=f"explicit tunnel requested; {health.detail}",
                explicit=True,
                health=health,
            )

        candidates: list[tuple[int, TunnelSpec, TunnelHealth]] = []
        for spec in self.registry.effective():
            if require_automatic and not spec.capabilities.automatic:
                continue
            health = self.probe(spec)
            if health.state == TunnelHealthState.UNAVAILABLE:
                continue
            health_penalty = {
                TunnelHealthState.HEALTHY: 0,
                TunnelHealthState.DEGRADED: 200,
                TunnelHealthState.UNKNOWN: 400,
                TunnelHealthState.UNAVAILABLE: 10000,
            }[health.state]
            score = spec.priority + self._policy_bonus(spec, policy) + health_penalty
            candidates.append((score, spec, health))

        if not candidates:
            raise RuntimeError("no usable automatic tunnel is available")
        candidates.sort(key=lambda item: (item[0], item[1].id))
        _, chosen, health = candidates[0]
        return TunnelSelection(
            tunnel_id=chosen.id,
            reason=f"auto-selected by policy={policy}; {health.detail}",
            explicit=False,
            health=health,
            alternatives=[item[1].id for item in candidates[1:]],
        )
