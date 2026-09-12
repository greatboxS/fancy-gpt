from __future__ import annotations

from dataclasses import dataclass, field

from .errors import BrowserTurnAmbiguousError


@dataclass
class ResponseBindingTracker:
    """Pure state machine for binding one logical assistant response to a turn."""

    baseline_ids: set[str]
    stable_polls: int = 3
    bound_id: str | None = None
    last_text: str | None = None
    stable_count: int = 0
    observed_ids: set[str] = field(default_factory=set)

    def observe(
        self,
        assistant_candidates: list[tuple[str, str]],
        *,
        stop_visible: bool,
    ) -> tuple[str, str] | None:
        candidates = [(identity, text) for identity, text in assistant_candidates if identity not in self.baseline_ids]
        identities = {identity for identity, _ in candidates}
        self.observed_ids.update(identities)

        if self.bound_id is None:
            if len(identities) > 1:
                raise BrowserTurnAmbiguousError(
                    "multiple new assistant logical turns appeared; refusing to guess response identity"
                )
            if not candidates:
                return None
            self.bound_id = candidates[0][0]
        elif any(identity != self.bound_id for identity in identities):
            raise BrowserTurnAmbiguousError("assistant response identity changed during one turn")

        current = [text for identity, text in candidates if identity == self.bound_id]
        if len(current) > 1:
            raise BrowserTurnAmbiguousError("duplicate DOM nodes exist for the bound assistant response")
        if not current:
            return None

        text = current[0]
        if text == self.last_text and not stop_visible:
            self.stable_count += 1
        else:
            self.stable_count = 0
        self.last_text = text

        if self.stable_count >= self.stable_polls:
            return self.bound_id, text
        return None
