from __future__ import annotations

from fancy_gpt.web.models import LayerHealth, LayerKind
from fancy_gpt.web.runtime import RuntimeRegistry
from fancy_gpt.web.transport import TransportRegistry

from .composition import TunnelCompositionValidator
from .models import TunnelSpec


class TunnelLayerInspector:
    """Static, side-effect-free health inspection for each tunnel layer."""

    def __init__(self) -> None:
        self.runtimes = RuntimeRegistry()
        self.transports = TransportRegistry()
        self.compositions = TunnelCompositionValidator(
            runtimes=self.runtimes,
            transports=self.transports,
        )

    def inspect(self, spec: TunnelSpec) -> list[LayerHealth]:
        layers: list[LayerHealth] = []
        for kind, component, getter in (
            (LayerKind.RUNTIME, spec.runtime.value, self.runtimes.get),
            (LayerKind.TRANSPORT, spec.transport.value, self.transports.get),
        ):
            try:
                item = getter(component)
                layers.append(LayerHealth(layer=kind, component=component, state="healthy", detail="contract registered", metadata=item.model_dump(mode="json")))
            except KeyError as exc:
                layers.append(LayerHealth(layer=kind, component=component, state="unavailable", detail=str(exc)))
        check = self.compositions.check(spec)
        layers.append(LayerHealth(
            layer=LayerKind.COMPOSITION,
            component=spec.id,
            state="healthy" if check.ok else "unavailable",
            detail="composition compatible" if check.ok else "; ".join(check.errors),
        ))
        return layers
