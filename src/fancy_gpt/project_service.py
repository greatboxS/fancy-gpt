from __future__ import annotations

import uuid
from pathlib import Path

from .project_models import (
    AcceptanceCriterion,
    AgentAssignment,
    AgentOutcome,
    AgentOutcomeStatus,
    AgentRole,
    ArtifactDraft,
    ConversationStrategy,
    CriterionStatus,
    DecisionRecord,
    EvidenceRecord,
    FindingRecord,
    FindingStatus,
    ProjectArtifactRecord,
    ProjectEvent,
    ProjectEventType,
    ProjectRecord,
    ProjectSnapshot,
    ProjectStatus,
    ProjectTarget,
    RelevantProjectContext,
    SessionRecord,
    SessionState,
    TeamCyclePlan,
    WorkExecutionMode,
    WorkActivationCondition,
    WorkItem,
    WorkItemState,
    is_chatgpt_conversation,
)
from .project_store import ProjectStore, utc_now
from .relevance import RelevanceSufficiencyPolicy, ResponseIntent, relevant_subset


class ProjectService:
    def __init__(self, root: Path) -> None:
        self.store = ProjectStore(root)

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def create_project(
        self,
        *,
        name: str,
        target: str,
        repo_root: str = ".",
        acceptance: list[AcceptanceCriterion] | None = None,
        project_id: str | None = None,
    ) -> ProjectRecord:
        now = utc_now()
        normalized_acceptance = [
            item if item.evidence_required else item.model_copy(update={"evidence_required": [item.statement]})
            for item in (acceptance or [])
        ]
        record = ProjectRecord(
            project_id=project_id or self._id("project"),
            name=name,
            repo_root=repo_root,
            target=ProjectTarget(statement=target, acceptance_criteria=normalized_acceptance),
            status=ProjectStatus.ACTIVE,
            principles=RelevanceSufficiencyPolicy(),
            default_response_intent=ResponseIntent.FOCUSED,
            created_at=now,
            updated_at=now,
        )
        self.store.create(record)
        return record

    def add_work_item(
        self,
        project_id: str,
        *,
        title: str,
        objective: str,
        role: AgentRole,
        execution_mode: WorkExecutionMode = WorkExecutionMode.MODEL,
        dependencies: list[str] | None = None,
        expected_outputs: list[str] | None = None,
        acceptance_notes: list[str] | None = None,
        required_for_completion: bool = True,
        activation_condition: WorkActivationCondition = WorkActivationCondition.ALWAYS,
        work_item_id: str | None = None,
    ) -> WorkItem:
        snapshot = self.snapshot(project_id)
        dependencies = dependencies or []
        known = {item.work_item_id for item in snapshot.work_items}
        unknown = set(dependencies) - known
        if unknown:
            raise ValueError(f"unknown work-item dependencies: {sorted(unknown)}")
        completed = {item.work_item_id for item in snapshot.work_items if item.state in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED}}
        state = WorkItemState.READY if set(dependencies).issubset(completed) else WorkItemState.BLOCKED
        item = WorkItem(
            work_item_id=work_item_id or self._id("work"),
            title=title,
            objective=objective,
            role=role,
            execution_mode=execution_mode,
            state=state,
            dependencies=dependencies,
            expected_outputs=expected_outputs or [],
            acceptance_notes=acceptance_notes or [],
            required_for_completion=required_for_completion,
            activation_condition=activation_condition,
        )
        self.store.append(project_id, ProjectEventType.WORK_ITEM_CREATED, item.model_dump(mode="json"))
        return item

    def set_work_item_state(self, project_id: str, work_item_id: str, state: WorkItemState, *, result_summary: str | None = None) -> WorkItem:
        current = self.snapshot(project_id).work_item(work_item_id)
        updated = current.model_copy(update={"state": state, "result_summary": result_summary if result_summary is not None else current.result_summary})
        self.store.append(project_id, ProjectEventType.WORK_ITEM_STATE_CHANGED, updated.model_dump(mode="json"))
        return updated

    def start_session(
        self,
        project_id: str,
        work_item_id: str,
        *,
        conversation_strategy: ConversationStrategy | None = None,
        conversation_binding: str | None = None,
    ) -> SessionRecord:
        snapshot = self.snapshot(project_id)
        item = snapshot.work_item(work_item_id)
        strategy = conversation_strategy or self.default_conversation_strategy(item.role)
        if conversation_binding is None and strategy == ConversationStrategy.RESUME:
            # Resume the newest persisted ChatGPT thread for the same role when one exists.
            # If this is the first role session, use a logical binding; the browser runtime
            # will create a normal persistent thread and return its real ChatGPT URL.
            candidates = [
                session for session in snapshot.sessions
                if session.role == item.role and session.conversation_binding
            ]
            real = [session for session in candidates if is_chatgpt_conversation(str(session.conversation_binding))]
            if real:
                real.sort(key=lambda session: session.started_at, reverse=True)
                conversation_binding = real[0].conversation_binding
            else:
                conversation_binding = f"project:{project_id}:role:{item.role.value}"
        elif conversation_binding is None and strategy == ConversationStrategy.FORK:
            conversation_binding = f"project:{project_id}:work:{work_item_id}"
        session = SessionRecord(
            session_id=self._id("session"),
            project_id=project_id,
            work_item_id=work_item_id,
            role=item.role,
            state=SessionState.OPEN,
            conversation_strategy=strategy,
            conversation_binding=conversation_binding,
            started_at=utc_now(),
        )
        self.store.append(project_id, ProjectEventType.SESSION_STARTED, session.model_dump(mode="json"))
        self.set_work_item_state(project_id, work_item_id, WorkItemState.RUNNING)
        return session

    def session(self, project_id: str, session_id: str) -> SessionRecord:
        snapshot = self.snapshot(project_id)
        existing = next((item for item in snapshot.sessions if item.session_id == session_id), None)
        if existing is None:
            raise KeyError(session_id)
        return existing

    def bind_session_conversation(self, project_id: str, session_id: str, binding: str) -> SessionRecord:
        if not is_chatgpt_conversation(binding):
            raise ValueError("conversation binding must be a resumable ChatGPT conversation")
        existing = self.session(project_id, session_id)
        updated = existing.model_copy(update={"conversation_binding": binding})
        self.store.append(project_id, ProjectEventType.SESSION_CONVERSATION_BOUND, updated.model_dump(mode="json"))
        return updated

    def finish_session(
        self,
        project_id: str,
        session_id: str,
        *,
        summary: str,
        failed: bool = False,
        work_state: WorkItemState | None = None,
    ) -> SessionRecord:
        snapshot = self.snapshot(project_id)
        existing = next((item for item in snapshot.sessions if item.session_id == session_id), None)
        if existing is None:
            raise KeyError(session_id)
        updated = existing.model_copy(update={
            "state": SessionState.FAILED if failed else SessionState.COMPLETE,
            "summary": summary,
            "ended_at": utc_now(),
        })
        self.store.append(project_id, ProjectEventType.SESSION_FINISHED, updated.model_dump(mode="json"))
        self.set_work_item_state(
            project_id,
            existing.work_item_id,
            work_state or (WorkItemState.FAILED if failed else WorkItemState.DONE),
            result_summary=summary,
        )
        return updated

    def record_decision(self, project_id: str, *, statement: str, rationale: str, source_session_id: str | None = None) -> DecisionRecord:
        decision = DecisionRecord(
            decision_id=self._id("decision"),
            statement=statement,
            rationale=rationale,
            source_session_id=source_session_id,
            recorded_at=utc_now(),
        )
        self.store.append(project_id, ProjectEventType.DECISION_RECORDED, decision.model_dump(mode="json"))
        return decision

    def record_evidence(self, project_id: str, *, claim: str, source: str, locator: str | None = None, source_session_id: str | None = None) -> EvidenceRecord:
        evidence = EvidenceRecord(
            evidence_id=self._id("evidence"),
            claim=claim,
            source=source,
            locator=locator,
            source_session_id=source_session_id,
            recorded_at=utc_now(),
        )
        self.store.append(project_id, ProjectEventType.EVIDENCE_RECORDED, evidence.model_dump(mode="json"))
        return evidence

    def record_finding(
        self,
        project_id: str,
        *,
        claim: str,
        impact: str,
        severity: str = "medium",
        required_action: str | None = None,
        source_session_id: str | None = None,
    ) -> FindingRecord:
        finding = FindingRecord(
            finding_id=self._id("finding"),
            severity=severity,
            claim=claim,
            impact=impact,
            required_action=required_action,
            source_session_id=source_session_id,
            recorded_at=utc_now(),
        )
        self.store.append(project_id, ProjectEventType.FINDING_RECORDED, finding.model_dump(mode="json"))
        return finding

    def resolve_finding(self, project_id: str, finding_id: str, *, resolution: str) -> FindingRecord:
        snapshot = self.snapshot(project_id)
        existing = next((item for item in snapshot.findings if item.finding_id == finding_id), None)
        if existing is None:
            raise KeyError(finding_id)
        updated = existing.model_copy(update={
            "status": FindingStatus.RESOLVED,
            "resolution": resolution,
            "resolved_at": utc_now(),
        })
        self.store.append(project_id, ProjectEventType.FINDING_RESOLVED, updated.model_dump(mode="json"))
        return updated

    def record_artifact(
        self,
        project_id: str,
        *,
        path: str,
        kind: str = "file",
        sha256: str | None = None,
        description: str | None = None,
        source_session_id: str | None = None,
    ) -> ProjectArtifactRecord:
        artifact = ProjectArtifactRecord(
            artifact_id=self._id("artifact"),
            path=path,
            kind=kind,
            sha256=sha256,
            description=description,
            source_session_id=source_session_id,
            recorded_at=utc_now(),
        )
        self.store.append(project_id, ProjectEventType.ARTIFACT_RECORDED, artifact.model_dump(mode="json"))
        return artifact

    def update_criterion(self, project_id: str, criterion_id: str, *, status: CriterionStatus, evidence_ids: list[str] | None = None) -> AcceptanceCriterion:
        snapshot = self.snapshot(project_id)
        criterion = next((item for item in snapshot.project.target.acceptance_criteria if item.id == criterion_id), None)
        if criterion is None:
            raise KeyError(criterion_id)
        evidence_ids = evidence_ids or criterion.evidence_ids
        known_evidence = {item.evidence_id for item in snapshot.evidence}
        unknown = set(evidence_ids) - known_evidence
        if unknown:
            raise ValueError(f"unknown evidence ids: {sorted(unknown)}")
        updated = criterion.model_copy(update={"status": status, "evidence_ids": evidence_ids})
        # Revalidate invariants after model_copy.
        updated = AcceptanceCriterion.model_validate(updated.model_dump(mode="json"))
        self.store.append(project_id, ProjectEventType.CRITERION_UPDATED, updated.model_dump(mode="json"))
        return updated

    def add_next_action(self, project_id: str, action: str) -> None:
        self.store.append(project_id, ProjectEventType.NEXT_ACTION_RECORDED, {"action": action})

    def snapshot(self, project_id: str) -> ProjectSnapshot:
        project = self.store.load_project(project_id)
        work: dict[str, WorkItem] = {}
        sessions: dict[str, SessionRecord] = {}
        decisions: list[DecisionRecord] = []
        evidence: list[EvidenceRecord] = []
        findings: dict[str, FindingRecord] = {}
        artifacts: list[ProjectArtifactRecord] = []
        criteria = {item.id: item for item in project.target.acceptance_criteria}
        next_actions: list[str] = []
        events = self.store.events(project_id)
        for event in events:
            payload = event.payload
            if event.event_type == ProjectEventType.WORK_ITEM_CREATED:
                item = WorkItem.model_validate(payload)
                work[item.work_item_id] = item
            elif event.event_type == ProjectEventType.WORK_ITEM_STATE_CHANGED:
                item = WorkItem.model_validate(payload)
                work[item.work_item_id] = item
            elif event.event_type in {
                ProjectEventType.SESSION_STARTED, ProjectEventType.SESSION_FINISHED, ProjectEventType.SESSION_CONVERSATION_BOUND
            }:
                item = SessionRecord.model_validate(payload)
                sessions[item.session_id] = item
                if item.work_item_id in work and item.session_id not in work[item.work_item_id].session_ids:
                    work[item.work_item_id] = work[item.work_item_id].model_copy(
                        update={"session_ids": work[item.work_item_id].session_ids + [item.session_id]}
                    )
            elif event.event_type == ProjectEventType.DECISION_RECORDED:
                decisions.append(DecisionRecord.model_validate(payload))
            elif event.event_type == ProjectEventType.EVIDENCE_RECORDED:
                evidence.append(EvidenceRecord.model_validate(payload))
            elif event.event_type in {ProjectEventType.FINDING_RECORDED, ProjectEventType.FINDING_RESOLVED}:
                item = FindingRecord.model_validate(payload)
                findings[item.finding_id] = item
            elif event.event_type == ProjectEventType.ARTIFACT_RECORDED:
                artifacts.append(ProjectArtifactRecord.model_validate(payload))
            elif event.event_type == ProjectEventType.CRITERION_UPDATED:
                item = AcceptanceCriterion.model_validate(payload)
                criteria[item.id] = item
            elif event.event_type == ProjectEventType.NEXT_ACTION_RECORDED:
                next_actions.append(str(payload.get("action", "")))

        # Materialize conditional work before readiness without mutating the journal merely by reading it.
        completed = {item_id for item_id, item in work.items() if item.state in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED}}
        open_findings = any(item.status == FindingStatus.OPEN for item in findings.values())
        for item_id, item in list(work.items()):
            deps_complete = set(item.dependencies).issubset(completed)
            if (
                item.activation_condition == WorkActivationCondition.OPEN_FINDINGS
                and deps_complete
                and not open_findings
                and item.state in {WorkItemState.BLOCKED, WorkItemState.READY}
            ):
                work[item_id] = item.model_copy(update={"state": WorkItemState.SKIPPED, "result_summary": "skipped: no open findings require resolution"})
                completed.add(item_id)
        for item_id, item in list(work.items()):
            if item.state == WorkItemState.BLOCKED and set(item.dependencies).issubset(completed):
                work[item_id] = item.model_copy(update={"state": WorkItemState.READY})

        target = project.target.model_copy(update={"acceptance_criteria": list(criteria.values())})
        all_criteria = list(criteria.values())
        criteria_complete = bool(all_criteria) and all(item.status == CriterionStatus.SATISFIED for item in all_criteria)
        required_work = [item for item in work.values() if item.required_for_completion]
        work_complete = not required_work or all(
            item.state in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED} for item in required_work
        )
        findings_complete = not any(item.status == FindingStatus.OPEN for item in findings.values())
        complete = criteria_complete and work_complete and findings_complete
        status = ProjectStatus.COMPLETE if complete else (ProjectStatus.ACTIVE if project.status == ProjectStatus.COMPLETE else project.status)
        materialized_project = project.model_copy(update={"target": target, "status": status})
        return ProjectSnapshot(
            project=materialized_project,
            work_items=list(work.values()),
            sessions=list(sessions.values()),
            decisions=decisions,
            evidence=evidence,
            findings=list(findings.values()),
            artifacts=artifacts,
            next_actions=relevant_subset(next_actions),
            event_count=len(events),
        )

    def history(self, project_id: str, *, limit: int = 100) -> list[ProjectEvent]:
        if limit < 1 or limit > 10_000:
            raise ValueError("history limit must be between 1 and 10000")
        events = self.store.events(project_id)
        return events[-limit:]

    def relevant_context(self, project_id: str, work_item_id: str | None = None) -> RelevantProjectContext:
        snapshot = self.snapshot(project_id)
        current = snapshot.work_item(work_item_id) if work_item_id else None
        dependency_results: list[str] = []
        if current:
            dependency_ids = set(current.dependencies)
            dependency_results = [
                item.result_summary
                for item in snapshot.work_items
                if item.work_item_id in dependency_ids and item.result_summary
            ]
        open_items = [
            item for item in snapshot.work_items
            if item.state not in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED, WorkItemState.FAILED}
        ]
        # Decisions/evidence are already structured and compact; keep only records
        # tied to relevant sessions when possible, otherwise current project records.
        relevant_sessions = set(current.session_ids if current else [])
        relevant_sessions.update(
            sid
            for item in snapshot.work_items
            if current and item.work_item_id in current.dependencies
            for sid in item.session_ids
        )
        decisions = [item for item in snapshot.decisions if not item.source_session_id or item.source_session_id in relevant_sessions]
        evidence = [item for item in snapshot.evidence if not item.source_session_id or item.source_session_id in relevant_sessions]
        findings = [item for item in snapshot.findings if item.status == FindingStatus.OPEN and (not item.source_session_id or item.source_session_id in relevant_sessions)]
        artifacts = [item for item in snapshot.artifacts if not item.source_session_id or item.source_session_id in relevant_sessions]
        return RelevantProjectContext(
            project_id=project_id,
            target=snapshot.project.target.statement,
            acceptance_criteria=snapshot.project.target.acceptance_criteria,
            current_work_item=current,
            dependency_results=dependency_results,
            relevant_decisions=decisions,
            relevant_evidence=evidence,
            open_findings=findings,
            relevant_artifacts=artifacts,
            open_work_items=open_items,
            next_actions=snapshot.next_actions,
            principles=snapshot.project.principles,
        )

    def start_assignment(
        self,
        project_id: str,
        work_item_id: str,
        *,
        conversation_strategy: ConversationStrategy | None = None,
        conversation_binding: str | None = None,
    ) -> AgentAssignment:
        snapshot = self.snapshot(project_id)
        item = snapshot.work_item(work_item_id)
        if item.state not in {WorkItemState.READY, WorkItemState.BLOCKED}:
            raise ValueError(f"work item {work_item_id} is not assignable in state {item.state.value}")
        if item.state == WorkItemState.BLOCKED and item not in snapshot.ready_items():
            raise ValueError(f"work item {work_item_id} has unsatisfied dependencies")
        session = self.start_session(
            project_id,
            work_item_id,
            conversation_strategy=conversation_strategy,
            conversation_binding=conversation_binding,
        )
        context = self.relevant_context(project_id, work_item_id)
        return AgentAssignment(
            project_id=project_id,
            work_item_id=work_item_id,
            session=session,
            role=item.role,
            objective=item.objective,
            relevant_context=context,
            role_instructions=self.role_instructions(item.role),
        )

    @staticmethod
    def role_instructions(role: AgentRole) -> list[str]:
        common = [
            "Work toward the assigned objective and project target; do not broaden the task merely because adjacent work is interesting.",
            "Return structured decisions/evidence/findings instead of replaying raw reasoning history.",
            "Surface a blocking unknown when it materially prevents correct continuation.",
        ]
        specific = {
            AgentRole.PLANNER: ["Create dependency-ordered work only; do not pretend implementation has occurred."],
            AgentRole.RESEARCHER: ["Collect discriminating evidence and stop when the assigned unknowns are resolved sufficiently."],
            AgentRole.DESIGNER: ["Produce interfaces/contracts/trade-offs needed by implementation; avoid candidate-solution anchoring when independence is required."],
            AgentRole.IMPLEMENTER: ["Change only what the accepted design/work item requires and preserve established contracts."],
            AgentRole.REVIEWER: ["Independently report material defects/risks; omit stylistic or adjacent commentary that does not change correctness or release readiness."],
            AgentRole.VERIFIER: ["Judge acceptance criteria from concrete evidence; configuration intent is not runtime proof."],
        }
        return common + specific[role]

    def apply_agent_outcome(self, project_id: str, outcome: AgentOutcome) -> SessionRecord:
        """Persist one teammate handoff and close/transition its session atomically at service level.

        Both browser-model teammates and external MCP clients (Codex/Claude) use
        this contract so project history has one durable semantic shape.
        """
        session = self.session(project_id, outcome.session_id)
        if session.project_id != project_id:
            raise ValueError("agent outcome project mismatch")
        if session.work_item_id != outcome.work_item_id:
            raise ValueError("agent outcome work_item mismatch")
        if session.role != outcome.role:
            raise ValueError("agent outcome role mismatch")
        if session.state != SessionState.OPEN:
            raise ValueError("agent outcome session is not open")

        if outcome.conversation_binding:
            self.bind_session_conversation(project_id, session.session_id, outcome.conversation_binding)
        for item in outcome.decisions:
            self.record_decision(
                project_id, statement=item.statement, rationale=item.rationale,
                source_session_id=session.session_id
            )
        evidence_ref_map: dict[str, str] = {}
        for item in outcome.evidence:
            recorded = self.record_evidence(
                project_id, claim=item.claim, source=item.source, locator=item.locator,
                source_session_id=session.session_id
            )
            if item.ref:
                if item.ref in evidence_ref_map:
                    raise ValueError(f"duplicate evidence ref in agent outcome: {item.ref}")
                evidence_ref_map[item.ref] = recorded.evidence_id
        for item in outcome.findings:
            self.record_finding(
                project_id, severity=item.severity, claim=item.claim, impact=item.impact,
                required_action=item.required_action, source_session_id=session.session_id
            )
        for item in outcome.finding_resolutions:
            self.resolve_finding(project_id, item.finding_id, resolution=item.resolution)
        for item in outcome.artifacts:
            self.record_artifact(
                project_id, path=item.path, kind=item.kind, sha256=item.sha256,
                description=item.description, source_session_id=session.session_id
            )
        if outcome.criterion_assessments:
            if outcome.role != AgentRole.VERIFIER:
                raise ValueError("only verifier outcomes may update acceptance criteria")
            for assessment in outcome.criterion_assessments:
                unknown_refs = [ref for ref in assessment.evidence_refs if ref not in evidence_ref_map]
                if unknown_refs:
                    raise ValueError(f"unknown evidence refs in criterion assessment: {unknown_refs}")
                resolved_evidence = list(assessment.evidence_ids) + [evidence_ref_map[ref] for ref in assessment.evidence_refs]
                self.update_criterion(
                    project_id, assessment.criterion_id, status=assessment.status,
                    evidence_ids=resolved_evidence
                )
        for action in outcome.next_actions:
            self.add_next_action(project_id, action)

        if outcome.status == AgentOutcomeStatus.BLOCKED:
            work_state = WorkItemState.BLOCKED
        elif outcome.status == AgentOutcomeStatus.NEEDS_EXTERNAL_ACTION:
            work_state = WorkItemState.READY
        else:
            work_state = WorkItemState.DONE
        return self.finish_session(
            project_id, session.session_id, summary=outcome.summary, work_state=work_state
        )

    def cycle_plan(self, project_id: str) -> TeamCyclePlan:
        snapshot = self.snapshot(project_id)
        if snapshot.project.status == ProjectStatus.COMPLETE:
            return TeamCyclePlan(project_id=project_id, ready_work_items=[], complete=True, blocked=False, reason="all acceptance criteria are satisfied")
        ready = snapshot.ready_items()
        unfinished = [item for item in snapshot.work_items if item.state not in {WorkItemState.DONE, WorkItemState.VERIFIED, WorkItemState.SKIPPED}]
        blocked = bool(unfinished) and not ready and not any(item.state == WorkItemState.RUNNING for item in unfinished)
        reason = "ready work is available" if ready else ("work is blocked" if blocked else "work is running or awaiting evidence")
        return TeamCyclePlan(project_id=project_id, ready_work_items=ready, complete=False, blocked=blocked, reason=reason)

    @staticmethod
    def default_conversation_strategy(role: AgentRole) -> ConversationStrategy:
        if role in {AgentRole.REVIEWER, AgentRole.VERIFIER}:
            return ConversationStrategy.FRESH
        if role in {AgentRole.IMPLEMENTER, AgentRole.RESEARCHER}:
            return ConversationStrategy.RESUME
        return ConversationStrategy.FORK
