"""Context compaction for the model gateway.

The gateway renders a whole client transcript into one browser prompt, so a
long conversation eventually exceeds what the composer can usefully carry.
Truncating the tail would break tool loops, so compaction here works on
protocol-semantic units rather than characters:

* system/developer instructions and tool schemas are reserved, never dropped;
* a tool call and its result are one atomic unit and are kept or dropped
  together, never split;
* what is dropped is replaced by a summary that stays *data* - it is never
  promoted into the system instruction position;
* every compaction records its generation, source range, source digest, a
  manifest of what was dropped, and where the summary came from.

If the reserved parts alone do not fit, compaction fails loudly instead of
producing a transcript that looks fine and has lost the caller's intent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field


def units(text: str) -> int:
    """Rough unit estimate, consistent with the gateway's input accounting."""
    return max(1, len(text) // 4) if text else 0


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CompactionImpossible(ValueError):
    """The reserved content alone does not fit the budget."""


@dataclass(frozen=True)
class ContextBudget:
    """How much of the context window history may occupy.

    Output space and tool-loop space are reserved up front so a compacted
    prompt still leaves room for the answer and for the remaining tool turns.
    """

    total_units: int
    reserve_output_units: int = 8_000
    reserve_tool_loop_units: int = 8_000

    def history_allowance(self, *, instruction_units: int, tool_schema_units: int) -> int:
        allowance = (
            self.total_units
            - self.reserve_output_units
            - self.reserve_tool_loop_units
            - instruction_units
            - tool_schema_units
        )
        if allowance <= 0:
            raise CompactionImpossible(
                "context budget is exhausted by instructions, tool schemas, and reserved "
                f"output/tool-loop space: {instruction_units + tool_schema_units} units of "
                f"fixed content against a {self.total_units} unit budget"
            )
        return allowance


class DroppedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    role: str
    units: int
    digest: str
    preview: str = ""


class CompactionRecord(BaseModel):
    """Durable provenance for one compaction pass."""

    model_config = ConfigDict(extra="forbid")
    generation: int = 0
    source_range: tuple[int, int] = (0, 0)
    source_digest: str = ""
    source_message_count: int = 0
    kept_indexes: list[int] = Field(default_factory=list)
    dropped: list[DroppedItem] = Field(default_factory=list)
    summary: str = ""
    summary_provenance: str = ""
    units_before: int = 0
    units_after: int = 0

    @property
    def applied(self) -> bool:
        return bool(self.dropped)


@dataclass
class Segment:
    """One transcript message plus what compaction needs to know about it."""

    index: int
    role: str
    text: str
    call_ids: frozenset[str] = frozenset()
    result_ids: frozenset[str] = frozenset()
    group: int = -1
    pinned: bool = False

    @property
    def units(self) -> int:
        return units(self.text)


def _segments(messages: Sequence[Any]) -> list[Segment]:
    segments: list[Segment] = []
    for index, message in enumerate(messages):
        text = getattr(message, "text", "") or ""
        # Tool identity comes from the structured fields the normalizers
        # populated from the real protocol shape. It is deliberately NOT
        # re-parsed out of the rendered text, because message content is
        # untrusted and could otherwise forge a tool pairing.
        segments.append(
            Segment(
                index=index,
                role=getattr(message, "role", "user"),
                text=text,
                call_ids=frozenset(getattr(message, "tool_call_ids", ()) or ()),
                result_ids=frozenset(getattr(message, "tool_result_ids", ()) or ()),
            )
        )
    return segments


def _group_tool_pairs(segments: list[Segment]) -> None:
    """Assign a shared group id to every tool call and its result.

    Grouped segments are kept or dropped together, so a tool call can never be
    separated from the result that answers it.
    """
    owner: dict[str, int] = {}
    next_group = 0
    for segment in segments:
        ids = segment.call_ids | segment.result_ids
        if not ids:
            continue
        existing = [owner[item] for item in ids if item in owner]
        group = existing[0] if existing else next_group
        if not existing:
            next_group += 1
        segment.group = group
        for item in ids:
            owner[item] = group


def _unresolved_groups(segments: list[Segment]) -> set[int]:
    """Groups whose tool call has no matching result yet."""
    calls: dict[int, set[str]] = {}
    results: dict[int, set[str]] = {}
    for segment in segments:
        if segment.group < 0:
            continue
        calls.setdefault(segment.group, set()).update(segment.call_ids)
        results.setdefault(segment.group, set()).update(segment.result_ids)
    return {group for group, ids in calls.items() if ids - results.get(group, set())}


def _summarize(dropped: list[Segment]) -> str:
    """Build the replacement summary.

    Deliberately mechanical: it states what was removed rather than
    paraphrasing it, so nothing the model later reads can be mistaken for a new
    instruction from the operator.
    """
    lines = [f"{len(dropped)} earlier message(s) were removed to fit the context budget."]
    by_role: dict[str, int] = {}
    for segment in dropped:
        by_role[segment.role] = by_role.get(segment.role, 0) + 1
    lines.append("Removed by role: " + ", ".join(f"{role}={count}" for role, count in sorted(by_role.items())))
    for segment in dropped[:10]:
        preview = " ".join(segment.text.split())[:160]
        lines.append(f"- [{segment.index}] {segment.role}: {preview}")
    if len(dropped) > 10:
        lines.append(f"- ... and {len(dropped) - 10} more")
    return "\n".join(lines)


def plan_compaction(
    messages: Sequence[Any],
    *,
    budget: ContextBudget,
    instruction_units: int,
    tool_schema_units: int,
    generation: int = 0,
) -> tuple[list[int], CompactionRecord]:
    """Decide which message indexes survive.

    Returns the kept indexes in original order plus a durable record. Raises
    :class:`CompactionImpossible` when the content that must be preserved does
    not fit, rather than dropping it.
    """
    segments = _segments(messages)
    total_before = sum(segment.units for segment in segments)
    record = CompactionRecord(
        generation=generation,
        source_range=(0, max(0, len(segments) - 1)),
        source_message_count=len(segments),
        source_digest=_digest([[segment.role, segment.text] for segment in segments]),
        units_before=total_before,
    )

    allowance = budget.history_allowance(
        instruction_units=instruction_units, tool_schema_units=tool_schema_units
    )
    if total_before <= allowance:
        record.kept_indexes = [segment.index for segment in segments]
        record.units_after = total_before
        return record.kept_indexes, record

    _group_tool_pairs(segments)
    unresolved = _unresolved_groups(segments)

    # Preservation order. Everything pinned here is mandatory; if the pinned set
    # alone overflows, compaction is impossible and says so.
    for segment in segments:
        if segment.role in {"system", "developer"}:
            segment.pinned = True
    for segment in reversed(segments):
        # Current user intent: the most recent user message.
        if segment.role == "user":
            segment.pinned = True
            break
    for segment in segments:
        if segment.group in unresolved:
            segment.pinned = True

    pinned_units = sum(segment.units for segment in segments if segment.pinned)
    if pinned_units > allowance:
        raise CompactionImpossible(
            f"content that must be preserved needs {pinned_units} units but only {allowance} "
            "are available; the request cannot be compacted safely"
        )

    kept: set[int] = {segment.index for segment in segments if segment.pinned}
    remaining = allowance - pinned_units

    # Then fill backwards from the most recent tail, admitting whole tool
    # groups so a pair is never half admitted.
    by_group: dict[int, list[Segment]] = {}
    for segment in segments:
        if segment.group >= 0:
            by_group.setdefault(segment.group, []).append(segment)

    for segment in reversed(segments):
        if segment.index in kept:
            continue
        unit_group = by_group.get(segment.group, [segment]) if segment.group >= 0 else [segment]
        pending = [item for item in unit_group if item.index not in kept]
        cost = sum(item.units for item in pending)
        if cost <= remaining:
            remaining -= cost
            kept.update(item.index for item in pending)

    dropped_segments = [segment for segment in segments if segment.index not in kept]
    record.dropped = [
        DroppedItem(
            index=segment.index,
            role=segment.role,
            units=segment.units,
            digest=_digest([segment.role, segment.text]),
            preview=" ".join(segment.text.split())[:160],
        )
        for segment in dropped_segments
    ]
    record.summary = _summarize(dropped_segments)
    record.summary_provenance = (
        f"gateway-compaction/generation-{generation}; mechanical manifest of "
        f"{len(dropped_segments)} dropped message(s) from source digest {record.source_digest[:16]}"
    )
    record.kept_indexes = sorted(kept)
    record.units_after = sum(segment.units for segment in segments if segment.index in kept)
    return record.kept_indexes, record
