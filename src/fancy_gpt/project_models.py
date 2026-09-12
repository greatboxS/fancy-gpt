from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import StrictModel
from .relevance import RelevanceAssessment, RelevanceSufficiencyPolicy, ResponseIntent


class AgentRole(str, Enum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    DESIGNER = "designer"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class CriterionStatus(str, Enum):
    UNSATISFIED = "unsatisfied"
    PARTIAL = "partial"
    SATISFIED = "satisfied"


class WorkExecutionMode(str, Enum):
    MODEL = "model"
    EXTERNAL_AGENT = "external-agent"


class WorkItemState(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    NEEDS_REVIEW = "needs-review"
    NEEDS_FIX = "needs-fix"
    VERIFIED = "verified"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class WorkActivationCondition(str, Enum):
    ALWAYS = "always"
    OPEN_FINDINGS = "open-findings"


class SessionState(str, Enum):
    OPEN = "open"
    COMPLETE = "complete"
    FAILED = "failed"


class ConversationStrategy(str, Enum):
    FRESH = "fresh"
    RESUME = "resume"
    FORK = "fork"


class ProjectEventType(str, Enum):
    PROJECT_CREATED = "project-created"
    WORK_ITEM_CREATED = "work-item-created"
    WORK_ITEM_STATE_CHANGED = "work-item-state-changed"
    SESSION_STARTED = "session-started"
    SESSION_FINISHED = "session-finished"
    SESSION_CONVERSATION_BOUND = "session-conversation-bound"
    DECISION_RECORDED = "decision-recorded"
    EVIDENCE_RECORDED = "evidence-recorded"
    CRITERION_UPDATED = "criterion-updated"
    NEXT_ACTION_RECORDED = "next-action-recorded"
    FINDING_RECORDED = "finding-recorded"
    FINDING_RESOLVED = "finding-resolved"
    ARTIFACT_RECORDED = "artifact-recorded"


class AcceptanceCriterion(StrictModel):
    id: str
    statement: str
    evidence_required: list[str] = Field(default_factory=list)
    status: CriterionStatus = CriterionStatus.UNSATISFIED
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def satisfied_requires_evidence(self) -> "AcceptanceCriterion":
        if self.status == CriterionStatus.SATISFIED and self.evidence_required and not self.evidence_ids:
            raise ValueError("satisfied acceptance criterion requires evidence")
        return self


class ProjectTarget(StrictModel):
    statement: str = Field(min_length=3)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class ProjectRecord(StrictModel):
    project_id: str
    name: str
    repo_root: str
    target: ProjectTarget
    status: ProjectStatus = ProjectStatus.ACTIVE
    principles: RelevanceSufficiencyPolicy = Field(default_factory=RelevanceSufficiencyPolicy)
    default_response_intent: ResponseIntent = ResponseIntent.FOCUSED
    created_at: str
    updated_at: str


class WorkItem(StrictModel):
    work_item_id: str
    title: str
    objective: str
    role: AgentRole
    execution_mode: WorkExecutionMode = WorkExecutionMode.MODEL
    state: WorkItemState = WorkItemState.BLOCKED
    dependencies: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    acceptance_notes: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    result_summary: str | None = None
    required_for_completion: bool = True
    activation_condition: WorkActivationCondition = WorkActivationCondition.ALWAYS


class SessionRecord(StrictModel):
    session_id: str
    project_id: str
    work_item_id: str
    role: AgentRole
    state: SessionState = SessionState.OPEN
    conversation_strategy: ConversationStrategy = ConversationStrategy.FRESH
    conversation_binding: str | None = None
    summary: str | None = None
    started_at: str
    ended_at: str | None = None


class DecisionRecord(StrictModel):
    decision_id: str
    statement: str
    rationale: str
    source_session_id: str | None = None
    recorded_at: str


class EvidenceRecord(StrictModel):
    evidence_id: str
    claim: str
    source: str
    locator: str | None = None
    source_session_id: str | None = None
    recorded_at: str


class ProjectEvent(StrictModel):
    sequence: int
    event_type: ProjectEventType
    timestamp: str
    payload: dict[str, Any]


class FindingStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class FindingRecord(StrictModel):
    finding_id: str
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    claim: str
    impact: str
    required_action: str | None = None
    source_session_id: str | None = None
    status: FindingStatus = FindingStatus.OPEN
    recorded_at: str
    resolved_at: str | None = None
    resolution: str | None = None


class ProjectArtifactRecord(StrictModel):
    artifact_id: str
    path: str
    kind: str = "file"
    sha256: str | None = None
    description: str | None = None
    source_session_id: str | None = None
    recorded_at: str


class DecisionDraft(StrictModel):
    statement: str
    rationale: str


class EvidenceDraft(StrictModel):
    ref: str | None = None
    claim: str
    source: str
    locator: str | None = None


class FindingDraft(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    claim: str
    impact: str
    required_action: str | None = None




class FindingResolutionDraft(StrictModel):
    finding_id: str
    resolution: str


class ArtifactDraft(StrictModel):
    path: str
    kind: str = "file"
    sha256: str | None = None
    description: str | None = None


class CriterionAssessmentDraft(StrictModel):
    criterion_id: str
    status: CriterionStatus
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    rationale: str


class AgentOutcomeStatus(str, Enum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    NEEDS_EXTERNAL_ACTION = "needs-external-action"


class AgentOutcome(StrictModel):
    request_id: str
    session_id: str
    work_item_id: str
    role: AgentRole
    status: AgentOutcomeStatus
    summary: str
    decisions: list[DecisionDraft] = Field(default_factory=list)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    findings: list[FindingDraft] = Field(default_factory=list)
    finding_resolutions: list[FindingResolutionDraft] = Field(default_factory=list)
    artifacts: list[ArtifactDraft] = Field(default_factory=list)
    criterion_assessments: list[CriterionAssessmentDraft] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    relevance_assessment: RelevanceAssessment = Field(default_factory=RelevanceAssessment)
    conversation_binding: str | None = None


class RelevantProjectContext(StrictModel):
    project_id: str
    target: str
    acceptance_criteria: list[AcceptanceCriterion]
    current_work_item: WorkItem | None = None
    dependency_results: list[str] = Field(default_factory=list)
    relevant_decisions: list[DecisionRecord] = Field(default_factory=list)
    relevant_evidence: list[EvidenceRecord] = Field(default_factory=list)
    open_findings: list[FindingRecord] = Field(default_factory=list)
    relevant_artifacts: list[ProjectArtifactRecord] = Field(default_factory=list)
    open_work_items: list[WorkItem] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    principles: RelevanceSufficiencyPolicy = Field(default_factory=RelevanceSufficiencyPolicy)


class ProjectSnapshot(StrictModel):
    project: ProjectRecord
    work_items: list[WorkItem] = Field(default_factory=list)
    sessions: list[SessionRecord] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    findings: list[FindingRecord] = Field(default_factory=list)
    artifacts: list[ProjectArtifactRecord] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    event_count: int = 0

    def work_item(self, work_item_id: str) -> WorkItem:
        for item in self.work_items:
            if item.work_item_id == work_item_id:
                return item
        raise KeyError(work_item_id)

    def ready_items(self) -> list[WorkItem]:
        completed = {item.work_item_id for item in self.work_items if item.state in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED}}
        ready: list[WorkItem] = []
        for item in self.work_items:
            if item.state in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED, WorkItemState.RUNNING, WorkItemState.FAILED}:
                continue
            if set(item.dependencies).issubset(completed):
                ready.append(item.model_copy(update={"state": WorkItemState.READY}))
        return ready


class AgentAssignment(StrictModel):
    project_id: str
    work_item_id: str
    session: SessionRecord
    role: AgentRole
    objective: str
    relevant_context: RelevantProjectContext
    role_instructions: list[str] = Field(default_factory=list)



class TeamCyclePlan(StrictModel):
    project_id: str
    ready_work_items: list[WorkItem]
    complete: bool
    blocked: bool
    reason: str


class TeamStepStatus(str, Enum):
    MODEL_COMPLETED = "model-completed"
    EXTERNAL_ASSIGNMENT_REQUIRED = "external-assignment-required"
    PROJECT_COMPLETE = "project-complete"
    BLOCKED = "blocked"
    IDLE = "idle"


class TeamStepResult(StrictModel):
    project_id: str
    status: TeamStepStatus
    work_item: WorkItem | None = None
    assignment: AgentAssignment | None = None
    outcome: AgentOutcome | None = None
    reason: str
