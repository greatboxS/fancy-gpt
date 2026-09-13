from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .relevance import RelevanceAssessment, RelevanceSufficiencyPolicy, ResponseIntent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RequestMode(str, Enum):
    REVIEW = "review"
    DESIGN = "design"
    CONSULT = "consult"
    INVESTIGATE = "investigate"
    VERIFY = "verify"
    WRITE = "write"


class Freshness(str, Enum):
    STATIC = "static"
    VERSION_SPECIFIC = "version-specific"
    CURRENT = "current"


class RiskLevel(str, Enum):
    NORMAL = "normal"
    PRODUCTION = "production"
    SECURITY_CRITICAL = "security-critical"
    SAFETY_CRITICAL = "safety-critical"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class ArtifactRole(str, Enum):
    CONTEXT = "context"
    REQUIREMENT = "requirement"
    CANDIDATE_SOLUTION = "candidate-solution"
    EVIDENCE = "evidence"


class RequestState(str, Enum):
    NEW = "new"
    RUNNING_PLANNER = "running-planner"
    WAITING_PLANNER = "waiting-planner"
    RUNNING_FINAL = "running-final"
    WAITING_FINAL = "waiting-final"
    COMPLETE = "complete"
    FAILED = "failed"


class ChatPolicy(str, Enum):
    CONTINUE = "continue"
    NEW_CHAT = "new_chat"
    INDEPENDENT = "independent"
    TEMPORARY = "temporary"


class ChatKind(str, Enum):
    MAIN = "main"
    STANDARD = "standard"
    INDEPENDENT = "independent"


class ArtifactSpec(StrictModel):
    path: str | None = None
    content: str | None = None
    name: str | None = None
    kind: str = "text"
    role: ArtifactRole = ArtifactRole.CONTEXT
    priority: Priority = Priority.P1

    @model_validator(mode="after")
    def require_path_or_content(self) -> "ArtifactSpec":
        if not self.path and self.content is None:
            raise ValueError("artifact requires path or content")
        return self


class RawRequest(StrictModel):
    mode: RequestMode
    objective: str = Field(min_length=3)
    repo_root: str = "."
    domains: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(
        default_factory=lambda: [
            "**/.git/**",
            "**/.venv/**",
            "**/.fancy-gpt/**",
            "**/build/**",
            "**/dist/**",
            "**/.pytest_cache/**",
            "**/__pycache__/**",
            "**/node_modules/**",
            "**/*.egg-info/**",
        ]
    )
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    notes: str | None = None
    include_git_diff: bool = False
    freshness: Freshness = Freshness.VERSION_SPECIFIC
    risk: RiskLevel = RiskLevel.NORMAL
    max_context_bytes: int = Field(default=180_000, ge=4096, le=50_000_000)
    site: str = "chatgpt"
    tunnel: str | None = None
    tunnel_policy: str = "auto"
    conversation_id: str | None = None
    conversation_mode: Literal["temporary", "persistent"] = "persistent"
    session_id: str | None = None
    chat_id: str | None = None
    chat_policy: ChatPolicy = ChatPolicy.CONTINUE
    response_intent: ResponseIntent = ResponseIntent.AUTO
    relevance_policy: RelevanceSufficiencyPolicy = Field(default_factory=RelevanceSufficiencyPolicy)

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, value: list[str]) -> list[str]:
        return [item.strip().lower() for item in value if item.strip()]

    @field_validator("focus", "constraints", "questions")
    @classmethod
    def normalize_strings(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("tunnel_policy")
    @classmethod
    def validate_tunnel_policy(cls, value: str) -> str:
        allowed = {"auto", "prefer-extension", "prefer-native", "prefer-remote", "prefer-playwright", "prefer-cdp"}
        if value not in allowed:
            raise ValueError(f"unsupported tunnel_policy: {value}")
        return value


class SessionRecord(StrictModel):
    session_id: str
    title: str
    repo_root: str
    active_chat_id: str | None = None
    chat_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    closed_at: str | None = None


class ChatRecord(StrictModel):
    chat_id: str
    session_id: str
    title: str
    kind: ChatKind
    site: str = "chatgpt"
    conversation_id: str | None = None
    tunnel_id: str | None = None
    request_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    archived_at: str | None = None


class ChatResolution(StrictModel):
    policy: ChatPolicy
    site: str = "chatgpt"
    session_id: str | None = None
    chat_id: str | None = None
    conversation_id: str | None = None


class SessionCapabilities(StrictModel):
    schema_version: str = "1.0"
    planner_conversation: Literal["temporary"] = "temporary"
    final_chat_policies: list[ChatPolicy] = Field(default_factory=lambda: list(ChatPolicy))
    default_policy: ChatPolicy = ChatPolicy.CONTINUE
    default_session_scope: Literal["repo_root"] = "repo_root"
    supports_multiple_chats: bool = True
    supports_active_chat: bool = True
    supports_independent_review: bool = True
    supports_archiving: bool = True
    supports_cross_process_recovery: bool = True
    conversation_binding_stage: Literal["before_result_validation"] = "before_result_validation"


class Capability(StrictModel):
    name: str
    description: str
    category: Literal["local", "online"]
    enabled: bool = True


class ResearchQuestion(StrictModel):
    id: str
    question: str
    rationale: str
    priority: Priority = Priority.P1


class LocalContextRequirement(StrictModel):
    id: str
    description: str
    patterns: list[str] = Field(default_factory=list)
    exact_paths: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    priority: Priority = Priority.P1
    required: bool = False


class OnlineResearchTask(StrictModel):
    id: str
    objective: str
    queries: list[str] = Field(default_factory=list)
    source_priority: list[str] = Field(default_factory=list)
    freshness: Freshness = Freshness.VERSION_SPECIFIC
    expected_evidence: list[str] = Field(default_factory=list)


class SourceCandidate(StrictModel):
    category: str
    source: str
    authority: Literal["primary", "secondary", "community"]
    relevance: str
    url: str | None = None
    version_or_date: str | None = None


class EvidenceRequirement(StrictModel):
    id: str
    claim_area: str
    required_evidence: list[str]
    acceptance_rule: str


class ToolUsage(StrictModel):
    capability: str
    purpose: str
    required: bool = False


class ResearchBudget(StrictModel):
    max_search_queries: int = Field(default=10, ge=0, le=50)
    max_open_pages: int = Field(default=10, ge=0, le=50)
    max_context_bytes: int = Field(default=180_000, ge=4096, le=50_000_000)


class ReportContract(StrictModel):
    sections: list[str]
    require_citations: bool = True
    require_evidence_mapping: bool = True
    require_unknowns: bool = True
    require_validation_plan: bool = True
    style: str = "concise, technical, evidence-first"


class ResearchManifest(StrictModel):
    intent: list[RequestMode]
    domains: list[str]
    tags: list[str] = Field(default_factory=list)
    research_goal: str
    research_questions: list[ResearchQuestion]
    local_context_requirements: list[LocalContextRequirement] = Field(default_factory=list)
    online_research: list[OnlineResearchTask] = Field(default_factory=list)
    source_map: list[SourceCandidate] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirement] = Field(default_factory=list)
    tool_plan: list[ToolUsage] = Field(default_factory=list)
    freshness: Freshness
    missing_evidence: list[str] = Field(default_factory=list)
    report_contract: ReportContract
    research_budget: ResearchBudget = Field(default_factory=ResearchBudget)


class ContextArtifact(StrictModel):
    id: str
    source: Literal["filesystem", "inline", "git-diff"]
    path: str
    kind: str
    role: ArtifactRole = ArtifactRole.CONTEXT
    priority: Priority
    sha256: str
    size: int
    content: str


class ContextPack(StrictModel):
    repo_root: str = "<local-repo-root>"
    artifacts: list[ContextArtifact]
    total_bytes: int
    context_hash: str
    omitted: list[str] = Field(default_factory=list)
    revision: str | None = None
    satisfied_requirements: list[str] = Field(default_factory=list)


class SkillDefinition(StrictModel):
    name: str
    mode: RequestMode
    description: str
    purpose: str
    required_sections: list[str]
    research_hints: list[str] = Field(default_factory=list)
    evidence_hints: list[str] = Field(default_factory=list)
    quality_gates: list[str] = Field(default_factory=list)


class WorkflowDefinition(StrictModel):
    name: str
    mode: RequestMode
    description: str
    primary_skill: str
    component_skills: list[str]
    default_domains: list[str] = Field(default_factory=list)
    default_focus: list[str] = Field(default_factory=list)
    required_sections: list[str] = Field(default_factory=list)
    quality_gates: list[str] = Field(default_factory=list)


class DomainDefinition(StrictModel):
    name: str
    description: str
    functional_focus: list[str]
    source_priority: list[str]
    typical_artifacts: list[str] = Field(default_factory=list)
    default_capabilities: list[str] = Field(default_factory=list)


class RoutingDecision(StrictModel):
    route_kind: Literal["skill", "workflow"]
    route_name: str
    mode: RequestMode
    primary_skill: str
    component_skills: list[str]
    domains: list[str]
    focus: list[str]
    allowed_capabilities: list[str]
    source_priority: list[str]
    required_sections: list[str]
    quality_gates: list[str]


class ModelRequest(StrictModel):
    request_id: str
    stage: Literal["planner", "final", "focused", "agent"]
    title: str
    prompt: str
    response_schema: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractionRequired(StrictModel):
    status: Literal["interaction_required"] = "interaction_required"
    request_id: str
    stage: Literal["planner", "final", "focused", "agent"]
    provider: Literal["chatgpt-web-interactive"] = "chatgpt-web-interactive"
    prompt_file: str
    instructions: list[str]
    chat_url: str = "https://chatgpt.com/"


class AutomatedModelResponse(StrictModel):
    status: Literal["completed"] = "completed"
    request_id: str
    stage: Literal["planner", "final", "focused", "agent"]
    provider: str
    raw_text: str
    response_identity: str
    conversation_id: str | None = None
    # Shapes and counts of what the adapter saw, never page text. Recorded for
    # turns that worked as well as ones that did not: a stall is only
    # diagnosable against some idea of what a healthy turn looks like.
    diagnostics: dict | None = None


class EvidenceRef(StrictModel):
    source: str = Field(min_length=1)
    locator: str | None = None
    claim_supported: str = Field(min_length=3)


class Finding(StrictModel):
    id: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str
    claim: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    impact: str
    recommendation: str

    @model_validator(mode="after")
    def high_findings_need_evidence(self) -> "Finding":
        if self.severity in {"high", "critical"} and not self.evidence:
            raise ValueError("high/critical findings require evidence")
        return self


class OptionAnalysis(StrictModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=3)
    advantages: list[str] = Field(default_factory=list)
    disadvantages: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    when_to_choose: str | None = None


class Hypothesis(StrictModel):
    id: str
    hypothesis: str
    mechanism: str
    evidence_for: list[EvidenceRef] = Field(default_factory=list)
    evidence_against: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    next_discriminating_test: str | None = None

    @model_validator(mode="after")
    def unresolved_hypothesis_needs_next_test(self) -> "Hypothesis":
        if self.confidence < 0.95 and not self.next_discriminating_test:
            raise ValueError("unresolved hypothesis requires next_discriminating_test")
        return self


class Deliverable(StrictModel):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    content: str = Field(min_length=20)


class SourceRecord(StrictModel):
    title: str = Field(min_length=1)
    url: str | None = None
    authority: Literal["primary", "secondary", "community"] = "secondary"
    accessed_or_version: str | None = None
    used_for: list[str] = Field(default_factory=list)


class ValidationStep(StrictModel):
    step: str
    expected_evidence: str
    pass_condition: str


class ResearchTaskTrace(StrictModel):
    task_id: str
    status: Literal["completed", "blocked", "skipped"]
    source_urls: list[str] = Field(default_factory=list)
    note: str

    @model_validator(mode="after")
    def completed_needs_source(self) -> "ResearchTaskTrace":
        if self.status == "completed" and not self.source_urls:
            raise ValueError("completed online research task requires at least one source URL")
        return self


class EvidenceCoverage(StrictModel):
    requirement_id: str
    status: Literal["satisfied", "partial", "missing"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    note: str

    @model_validator(mode="after")
    def satisfied_needs_evidence(self) -> "EvidenceCoverage":
        if self.status == "satisfied" and not self.evidence:
            raise ValueError("satisfied evidence coverage requires concrete evidence")
        return self


class FinalReport(StrictModel):
    request_id: str
    mode: RequestMode
    status: Literal["complete"] = "complete"
    verdict: str
    executive_summary: str
    report_sections_completed: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    options: list[OptionAnalysis] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    recommendation: str | None = None
    assumptions_challenged: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    validation_plan: list[ValidationStep] = Field(default_factory=list)
    research_trace: list[ResearchTaskTrace] = Field(default_factory=list)
    evidence_coverage: list[EvidenceCoverage] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    relevance_assessment: RelevanceAssessment = Field(default_factory=RelevanceAssessment)

    @model_validator(mode="after")
    def mode_contract(self) -> "FinalReport":
        if self.mode == RequestMode.CONSULT:
            if len(self.options) < 2:
                raise ValueError("consult mode requires at least two real options")
            if any(not item.advantages or not item.disadvantages or not item.when_to_choose for item in self.options):
                raise ValueError("consult options require advantages, disadvantages, and when_to_choose guidance")
            if len({item.name.casefold() for item in self.options}) != len(self.options):
                raise ValueError("consult option names must be distinct")
        if self.mode == RequestMode.WRITE and not self.deliverables:
            raise ValueError("write mode requires at least one reusable deliverable")
        if self.mode == RequestMode.DESIGN and not self.deliverables:
            raise ValueError("design mode requires a complete design deliverable")
        if self.mode == RequestMode.INVESTIGATE and not self.hypotheses:
            raise ValueError("investigate mode requires structured hypotheses")
        if self.mode == RequestMode.VERIFY and not self.evidence_coverage:
            raise ValueError("verify mode requires evidence coverage")
        if self.mode == RequestMode.VERIFY and self.verdict.casefold() not in {"pass", "fail", "insufficient"}:
            raise ValueError("verify verdict must be pass, fail, or insufficient")
        if not self.validation_plan:
            raise ValueError("final report requires a concrete validation plan")
        return self


class RequestStatus(StrictModel):
    request_id: str
    kind: Literal["review", "focused", "agent", "gateway"] = "review"
    execution_id: str | None = None
    state: RequestState
    route_kind: Literal["skill", "workflow"]
    route_name: str
    skill: str
    mode: RequestMode
    objective: str
    created_at: str
    updated_at: str
    provider: str | None = None
    tunnel_id: str | None = None
    planner_prompt_file: str | None = None
    planner_response_file: str | None = None
    final_prompt_file: str | None = None
    final_response_file: str | None = None
    result_file: str | None = None
    error: str | None = None
    partial_text: str | None = None
    partial_text_updated_at: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
    chat_policy: ChatPolicy | None = None


class RecoveryHint(StrictModel):
    code: str
    message: str


class InspectedRequest(StrictModel):
    request_id: str
    kind: Literal["review", "focused", "agent", "gateway"] = "review"
    execution_id: str | None = None
    objective: str
    mode: RequestMode
    route_kind: Literal["skill", "workflow"]
    route_name: str
    skill: str
    chat_policy: ChatPolicy | None = None
    state: RequestState
    created_at: str
    updated_at: str
    provider: str | None = None
    tunnel_id: str | None = None
    conversation_id: str | None = None
    has_partial_text: bool = False
    partial_text: str | None = None
    partial_text_updated_at: str | None = None
    error: str | None = None
    recovery_hint: RecoveryHint | None = None
    response_file: str | None = None
    result_file: str | None = None


class InspectedChat(StrictModel):
    chat_id: str
    title: str
    kind: ChatKind
    is_active: bool
    is_archived: bool
    conversation_id: str | None = None
    conversation_binding_state: Literal["unbound", "bound", "pending", "unavailable"]
    tunnel_id: str | None = None
    request_count: int
    latest_request: InspectedRequest | None = None
    requests: list[InspectedRequest] = Field(default_factory=list)


class InspectedTunnel(StrictModel):
    tunnel_id: str | None = None
    state: Literal["healthy", "unavailable", "unknown"] = "unknown"
    browser_connected: bool | None = None
    bridge_reachable: bool | None = None
    detail: str | None = None


class SessionInspection(StrictModel):
    schema_version: str = "1.1"
    session_id: str
    title: str
    repo_root: str
    active_chat_id: str | None = None
    created_at: str
    updated_at: str
    closed_at: str | None = None
    state: Literal["open", "closed"]
    chat_count: int
    request_count: int
    chats: list[InspectedChat]
    tunnels: list[InspectedTunnel] = Field(default_factory=list)
