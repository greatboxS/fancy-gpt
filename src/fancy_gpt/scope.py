from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .models import RawRequest
from .relevance import ExpansionTrigger, RelevanceSufficiencyPolicy, ResponseIntent


class ScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    requested_questions: list[str] = Field(default_factory=list)
    requested_focus: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    response_intent: ResponseIntent = ResponseIntent.AUTO
    preserve_scope: bool = True
    expansion_allowed_only_for: list[ExpansionTrigger] = Field(default_factory=list)
    sufficiency_rule: str = (
        "Stop when the current intent is resolved well enough to make the requested decision or take the next required action; "
        "do not widen coverage merely to be exhaustive."
    )


class ScopeInterpreter:
    """Deterministic scope extraction. It never invents a broader task."""

    def build(self, request: RawRequest) -> ScopeContract:
        policy = request.relevance_policy
        return ScopeContract(
            objective=request.objective,
            requested_questions=list(request.questions),
            requested_focus=list(request.focus),
            constraints=list(request.constraints),
            response_intent=request.response_intent,
            preserve_scope=policy.preserve_scope,
            expansion_allowed_only_for=list(policy.expansion_triggers),
        )
