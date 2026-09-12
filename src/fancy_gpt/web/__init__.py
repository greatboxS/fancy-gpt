"""Layered web interaction contracts.

The web stack is deliberately split into independent layers:
site semantics -> browser runtime -> transport -> tunnel composition.
"""

from .models import LayerHealth, LayerKind

__all__ = ["LayerHealth", "LayerKind"]
