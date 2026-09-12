from __future__ import annotations

from pathlib import Path

from fancy_gpt.orchestrator import TeamOrchestrator
from fancy_gpt.project_models import (
    AcceptanceCriterion,
    AgentRole,
    ConversationStrategy,
    CriterionStatus,
    ProjectStatus,
    WorkItemState,
)
from fancy_gpt.project_service import ProjectService


def _service(tmp_path: Path) -> ProjectService:
    return ProjectService(tmp_path / "state")


def test_project_journal_persists_and_reduces_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(
        project_id="demo",
        name="Demo",
        target="Ship a verified feature",
        acceptance=[AcceptanceCriterion(id="ac-1", statement="Feature passes runtime test", evidence_required=["runtime result"])],
    )
    work = service.add_work_item(project.project_id, title="Research", objective="Find constraints", role=AgentRole.RESEARCHER)
    session = service.start_session(project.project_id, work.work_item_id)
    assert session.conversation_strategy == ConversationStrategy.RESUME
    service.finish_session(project.project_id, session.session_id, summary="Constraints established")
    service.record_decision(project.project_id, statement="Use interface A", rationale="Matches current contract", source_session_id=session.session_id)

    reloaded = _service(tmp_path).snapshot(project.project_id)
    assert reloaded.work_item(work.work_item_id).state == WorkItemState.DONE
    assert reloaded.work_item(work.work_item_id).result_summary == "Constraints established"
    assert len(reloaded.decisions) == 1
    assert reloaded.event_count >= 5


def test_developer_cycle_advances_by_dependencies(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(project_id="cycle", name="Cycle", target="Complete developer cycle")
    ids = TeamOrchestrator(service).bootstrap_developer_cycle(project.project_id)
    plan = TeamOrchestrator(service).continue_project(project.project_id)
    assert len(ids) == 8
    assert [item.title for item in plan.ready_work_items] == ["Discover and research"]

    first = plan.ready_work_items[0]
    session = service.start_session(project.project_id, first.work_item_id)
    service.finish_session(project.project_id, session.session_id, summary="Discovery complete")
    next_plan = service.cycle_plan(project.project_id)
    assert [item.title for item in next_plan.ready_work_items] == ["Design"]


def test_acceptance_completion_requires_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(
        project_id="evidence",
        name="Evidence",
        target="Prove behavior",
        acceptance=[AcceptanceCriterion(id="runtime", statement="Runtime behavior is proven", evidence_required=["runtime evidence"])],
    )
    evidence = service.record_evidence(project.project_id, claim="Behavior observed", source="test log")
    service.update_criterion(project.project_id, "runtime", status=CriterionStatus.SATISFIED, evidence_ids=[evidence.evidence_id])
    snapshot = service.snapshot(project.project_id)
    assert snapshot.project.status == ProjectStatus.COMPLETE


def test_relevant_context_does_not_replay_raw_session_history(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(project_id="context", name="Context", target="Keep state compact")
    a = service.add_work_item(project.project_id, title="A", objective="A", role=AgentRole.RESEARCHER)
    s = service.start_session(project.project_id, a.work_item_id)
    service.finish_session(project.project_id, s.session_id, summary="Only reduced result survives")
    b = service.add_work_item(project.project_id, title="B", objective="B", role=AgentRole.DESIGNER, dependencies=[a.work_item_id])
    context = service.relevant_context(project.project_id, b.work_item_id)
    assert context.dependency_results == ["Only reduced result survives"]
    dumped = context.model_dump_json()
    assert "raw" not in dumped.lower()


def test_independent_roles_default_to_fresh_conversations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.default_conversation_strategy(AgentRole.REVIEWER) == ConversationStrategy.FRESH
    assert service.default_conversation_strategy(AgentRole.VERIFIER) == ConversationStrategy.FRESH
    assert service.default_conversation_strategy(AgentRole.IMPLEMENTER) == ConversationStrategy.RESUME


def test_assignment_contains_reduced_context_and_role_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(project_id="assign", name="Assign", target="Ship a bounded change")
    work = service.add_work_item(project.project_id, title="Review", objective="Review only material defects", role=AgentRole.REVIEWER)
    assignment = service.start_assignment(project.project_id, work.work_item_id)
    assert assignment.role == AgentRole.REVIEWER
    assert assignment.session.conversation_strategy == ConversationStrategy.FRESH
    assert any("material" in line for line in assignment.role_instructions)
    assert assignment.relevant_context.target == "Ship a bounded change"


def test_session_conversation_binding_is_persisted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create_project(project_id="binding", name="Binding", target="Keep a persistent thread")
    work = service.add_work_item(project.project_id, title="Implement", objective="Continue implementation", role=AgentRole.IMPLEMENTER)
    session = service.start_session(project.project_id, work.work_item_id)
    assert session.conversation_strategy == ConversationStrategy.RESUME
    assert session.conversation_binding.startswith("project:")
    service.bind_session_conversation(project.project_id, session.session_id, "https://chatgpt.com/c/test-thread")
    reloaded = _service(tmp_path).session(project.project_id, session.session_id)
    assert reloaded.conversation_binding == "https://chatgpt.com/c/test-thread"
