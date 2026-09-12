from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field


class ExpansionTrigger(str, Enum):
    CORRECTNESS = "changes-correctness"
    MATERIAL_RISK = "material-risk"
    DECISION_QUALITY = "decision-quality"
    CONFIDENCE = "confidence"
    NEXT_ACTION = "next-required-action"
    BLOCKING_UNKNOWN = "blocking-unknown"


class ResponseIntent(str, Enum):
    AUTO = "auto"
    REFERENCE = "reference"
    FOCUSED = "focused"
    DEEP = "deep"


class RelevanceSufficiencyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    """Semantic output discipline shared by every FancyGPT session.

    This intentionally does not use a word/token ceiling as the primary control.
    The policy decides whether information earns its place by changing the
    current answer, decision, risk, confidence, or next required action.
    """

    preserve_scope: bool = True
    answer_actual_question: bool = True
    stop_when_sufficient: bool = True
    expansion_triggers: list[ExpansionTrigger] = Field(
        default_factory=lambda: [
            ExpansionTrigger.CORRECTNESS,
            ExpansionTrigger.MATERIAL_RISK,
            ExpansionTrigger.DECISION_QUALITY,
            ExpansionTrigger.CONFIDENCE,
            ExpansionTrigger.NEXT_ACTION,
            ExpansionTrigger.BLOCKING_UNKNOWN,
        ]
    )
    avoid_by_default: list[str] = Field(
        default_factory=lambda: [
            "background that is not required to use the answer correctly",
            "alternatives when no choice is being made",
            "implementation guidance for a factual-only question",
            "test plans when verification is not part of the task",
            "adjacent architecture commentary that does not change the decision",
            "repetition of context already established in the project state",
        ]
    )

    def instructions(self, intent: ResponseIntent = ResponseIntent.AUTO) -> list[str]:
        rules = [
            "Answer the actual question, not the broader surrounding topic.",
            "Preserve the caller's scope unless expansion is materially required.",
            "Add information only when it changes correctness, decision quality, material risk, confidence, a blocking unknown, or the next required action.",
            "Do not add background knowledge unless the answer would otherwise be misunderstood or misused.",
            "Do not enumerate alternatives unless choosing among alternatives is part of the current decision.",
            "Do not add implementation or validation guidance unless it is requested or materially changes the usable answer.",
            "When a nearby issue is interesting but non-material, omit it rather than mentioning it as an aside.",
            "Stop when the current intent is sufficiently resolved; completeness means sufficient for this intent, not exhaustive coverage of the topic.",
        ]
        if intent == ResponseIntent.REFERENCE:
            rules.append("Treat this as a reference lookup: return the fact/decision and only the minimum context required to use it correctly.")
        elif intent == ResponseIntent.FOCUSED:
            rules.append("Keep the reasoning bounded to the requested decision/problem and material dependencies.")
        elif intent == ResponseIntent.DEEP:
            rules.append("Depth is welcome where it contributes to the requested analysis, but unrelated breadth is still noise.")
        return rules


def semantic_policy_text(policy: RelevanceSufficiencyPolicy, intent: ResponseIntent = ResponseIntent.AUTO) -> str:
    trigger_names = ", ".join(item.value for item in policy.expansion_triggers)
    rules = "\n".join(f"- {item}" for item in policy.instructions(intent))
    return (
        f"{rules}\n"
        f"- Permitted expansion triggers: {trigger_names}.\n"
        "- Before keeping a paragraph/section, ask: if it were removed, would correctness, the decision, material risk, confidence, a blocker, or the next required action become worse? If not, remove it."
    )


def relevant_subset(items: Iterable[str]) -> list[str]:
    """Stable de-duplication used by state reducers; not a token-budget heuristic."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


class ScopeExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    topic: str
    trigger: ExpansionTrigger
    rationale: str


class RelevanceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    within_requested_scope: bool = True
    necessary_expansions: list[ScopeExpansion] = Field(default_factory=list)
    omitted_non_material_topics: list[str] = Field(default_factory=list)

