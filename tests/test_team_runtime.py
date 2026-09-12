from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.orchestrator import TeamOrchestrator
from fancy_gpt.project_models import (
    AgentRole,
    TeamStepStatus,
    WorkExecutionMode,
    WorkItemState,
)
from fancy_gpt.project_runner import ProjectRunner
from fancy_gpt.project_service import ProjectService
from fancy_gpt.providers import ChatGPTWebAutomationProvider


def _response(*, summary: str, status: str = "complete", decisions=None, evidence=None, findings=None, artifacts=None, criterion_assessments=None, next_actions=None):
    def build(turn):
        return json.dumps({
            "request_id": turn.request_id,
            "session_id": CURRENT_SESSION[0],
            "work_item_id": CURRENT_WORK[0],
            "role": CURRENT_ROLE[0],
            "status": status,
            "summary": summary,
            "decisions": decisions or [],
            "evidence": evidence or [],
            "findings": findings or [],
            "artifacts": artifacts or [],
            "criterion_assessments": criterion_assessments or [],
            "next_actions": next_actions or [],
            "confidence": 0.91,
            "relevance_assessment": {
                "within_requested_scope": True,
                "necessary_expansions": [],
                "omitted_non_material_topics": ["unrelated background"],
            },
        })
    return build


CURRENT_SESSION = [""]
CURRENT_WORK = [""]
CURRENT_ROLE = [""]


class ScriptedManager:
    def __init__(self, responses):
        self.driver = FakeBrowserDriver(responses)

    def select(self, **_kwargs):
        return SimpleNamespace(tunnel_id="fake-team-tunnel")

    def provider(self, _selection):
        provider = ChatGPTWebAutomationProvider(self.driver, tunnel_id="fake-team-tunnel")
        original_execute = provider.execute

        def execute(request):
            CURRENT_SESSION[0] = str(request.metadata["session_id"])
            CURRENT_WORK[0] = str(request.metadata["work_item_id"])
            CURRENT_ROLE[0] = str(request.metadata["agent_role"])
            return original_execute(request)

        provider.execute = execute  # type: ignore[method-assign]
        return provider


def test_team_cycle_runs_model_roles_until_external_implementer(tmp_path: Path) -> None:
    service = ProjectService(tmp_path / "state")
    project = service.create_project(project_id="team", name="Team", target="Deliver a verified change")
    TeamOrchestrator(service).bootstrap_developer_cycle(project.project_id)
    manager = ScriptedManager([
        _response(
            summary="Relevant constraints established.",
            decisions=[{"statement": "Preserve tunnel abstraction", "rationale": "It is an established contract."}],
            evidence=[{"claim": "Baseline tests pass", "source": "pytest", "locator": "baseline"}],
        ),
        _response(summary="Design contract completed."),
        _response(summary="Implementation plan is dependency ordered."),
    ])
    runner = ProjectRunner(service, manager=manager)  # type: ignore[arg-type]
    results = runner.run_until_pause(project.project_id, max_steps=8)

    assert [item.status for item in results] == [
        TeamStepStatus.MODEL_COMPLETED,
        TeamStepStatus.MODEL_COMPLETED,
        TeamStepStatus.MODEL_COMPLETED,
        TeamStepStatus.EXTERNAL_ASSIGNMENT_REQUIRED,
    ]
    external = results[-1]
    assert external.assignment is not None
    assert external.assignment.role == AgentRole.IMPLEMENTER
    assert external.work_item is not None
    assert external.work_item.execution_mode == WorkExecutionMode.EXTERNAL_AGENT

    snapshot = ProjectService(tmp_path / "state").snapshot(project.project_id)
    assert len(snapshot.decisions) == 1
    assert len(snapshot.evidence) == 1
    assert snapshot.work_item(external.work_item.work_item_id).state == WorkItemState.RUNNING


def test_agent_outcome_persists_findings_artifacts_and_next_actions(tmp_path: Path) -> None:
    service = ProjectService(tmp_path / "state")
    project = service.create_project(project_id="records", name="Records", target="Keep durable team records")
    work = service.add_work_item(
        project.project_id,
        title="Review",
        objective="Find only material defects",
        role=AgentRole.REVIEWER,
    )
    manager = ScriptedManager([
        _response(
            summary="One material issue found.",
            findings=[{
                "severity": "high",
                "claim": "Site readiness is not verified",
                "impact": "Resolver may select an unusable browser",
                "required_action": "Probe ChatGPT site readiness before model execution",
            }],
            artifacts=[{
                "path": "TEST_REPORT.md",
                "kind": "report",
                "sha256": "a" * 64,
                "description": "review evidence report",
            }],
            next_actions=["Implement site readiness probe"],
        )
    ])
    result = ProjectRunner(service, manager=manager).run_next(project.project_id)  # type: ignore[arg-type]
    assert result.status == TeamStepStatus.MODEL_COMPLETED
    snapshot = ProjectService(tmp_path / "state").snapshot(project.project_id)
    assert snapshot.findings[0].claim == "Site readiness is not verified"
    assert snapshot.artifacts[0].path == "TEST_REPORT.md"
    assert snapshot.next_actions == ["Implement site readiness probe"]


def test_resume_session_reuses_latest_real_role_thread(tmp_path: Path) -> None:
    service = ProjectService(tmp_path / "state")
    project = service.create_project(project_id="resume", name="Resume", target="Continue a persistent research thread")
    first = service.add_work_item(project.project_id, title="R1", objective="Research first", role=AgentRole.RESEARCHER)
    s1 = service.start_session(project.project_id, first.work_item_id)
    service.bind_session_conversation(project.project_id, s1.session_id, "https://chatgpt.com/c/persistent-thread")
    service.finish_session(project.project_id, s1.session_id, summary="First research complete")
    second = service.add_work_item(project.project_id, title="R2", objective="Research follow-up", role=AgentRole.RESEARCHER)
    s2 = service.start_session(project.project_id, second.work_item_id)
    assert s2.conversation_binding == "https://chatgpt.com/c/persistent-thread"


def test_external_assignment_can_be_completed_by_mcp_client(tmp_path: Path) -> None:
    service = ProjectService(tmp_path / "state")
    project = service.create_project(project_id="external", name="External", target="Let Codex implement")
    item = service.add_work_item(
        project.project_id,
        title="Implement",
        objective="Modify source and tests",
        role=AgentRole.IMPLEMENTER,
        execution_mode=WorkExecutionMode.EXTERNAL_AGENT,
    )
    result = ProjectRunner(service, manager=ScriptedManager([])).run_next(project.project_id)  # type: ignore[arg-type]
    assert result.status == TeamStepStatus.EXTERNAL_ASSIGNMENT_REQUIRED
    assert result.assignment is not None
    service.finish_session(project.project_id, result.assignment.session.session_id, summary="Codex changed source and tests")
    assert service.snapshot(project.project_id).work_item(item.work_item_id).state == WorkItemState.DONE


def test_verifier_can_complete_target_only_with_existing_evidence(tmp_path: Path) -> None:
    from fancy_gpt.project_models import AcceptanceCriterion, CriterionStatus, ProjectStatus

    service = ProjectService(tmp_path / "state")
    project = service.create_project(
        project_id="verify-complete",
        name="Verify",
        target="Complete only from durable evidence",
        acceptance=[AcceptanceCriterion(id="ac-1", statement="Runtime passes", evidence_required=["runtime proof"])],
    )
    evidence = service.record_evidence(project.project_id, claim="Runtime passed", source="pytest", locator="test_runtime")
    service.add_work_item(
        project.project_id,
        title="Verify",
        objective="Judge acceptance from existing evidence",
        role=AgentRole.VERIFIER,
    )
    manager = ScriptedManager([
        _response(
            summary="Acceptance criterion is proven by existing runtime evidence.",
            criterion_assessments=[{
                "criterion_id": "ac-1",
                "status": CriterionStatus.SATISFIED.value,
                "evidence_ids": [evidence.evidence_id],
                "rationale": "The recorded pytest evidence directly proves the criterion.",
            }],
        )
    ])
    result = ProjectRunner(service, manager=manager).run_next(project.project_id)  # type: ignore[arg-type]
    assert result.status == TeamStepStatus.MODEL_COMPLETED
    snapshot = service.snapshot(project.project_id)
    assert snapshot.project.status == ProjectStatus.COMPLETE


def test_open_finding_blocks_project_completion_until_resolved(tmp_path: Path) -> None:
    from fancy_gpt.project_models import AcceptanceCriterion, CriterionStatus, ProjectStatus

    service = ProjectService(tmp_path / "state")
    project = service.create_project(
        project_id="finding-gate",
        name="Finding gate",
        target="Do not ship with open material findings",
        acceptance=[AcceptanceCriterion(id="ac-1", statement="Tests pass", evidence_required=["test evidence"])],
    )
    work = service.add_work_item(project.project_id, title="Work", objective="Finish work", role=AgentRole.VERIFIER)
    session = service.start_session(project.project_id, work.work_item_id)
    service.finish_session(project.project_id, session.session_id, summary="work finished")
    evidence = service.record_evidence(project.project_id, claim="Tests pass", source="pytest")
    service.update_criterion(project.project_id, "ac-1", status=CriterionStatus.SATISFIED, evidence_ids=[evidence.evidence_id])
    finding = service.record_finding(project.project_id, claim="Race remains", impact="Can corrupt state", severity="high")
    assert service.snapshot(project.project_id).project.status == ProjectStatus.ACTIVE
    service.resolve_finding(project.project_id, finding.finding_id, resolution="Generation guard added and tested")
    assert service.snapshot(project.project_id).project.status == ProjectStatus.COMPLETE
