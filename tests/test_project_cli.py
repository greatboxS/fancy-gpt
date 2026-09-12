from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fancy_gpt.cli import app

runner = CliRunner()


def test_project_cli_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "state"
    created = runner.invoke(app, [
        "project", "init", "Demo", "--target", "Ship verified feature",
        "--acceptance", "Runtime test passes", "--id", "demo", "--workdir", str(state),
    ])
    assert created.exit_code == 0, created.stdout
    assert json.loads(created.stdout)["project_id"] == "demo"

    boot = runner.invoke(app, ["project", "bootstrap", "demo", "--workdir", str(state)])
    assert boot.exit_code == 0, boot.stdout
    assert len(json.loads(boot.stdout)["work_item_ids"]) == 8

    status = runner.invoke(app, ["project", "status", "demo", "--workdir", str(state)])
    assert status.exit_code == 0, status.stdout
    payload = json.loads(status.stdout)
    assert payload["project"]["target"]["statement"] == "Ship verified feature"
    assert len(payload["work_items"]) == 8

    plan = runner.invoke(app, ["project", "continue", "demo", "--workdir", str(state)])
    assert plan.exit_code == 0, plan.stdout
    assert json.loads(plan.stdout)["ready_work_items"][0]["title"] == "Discover and research"


def test_project_context_command_is_reduced_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert runner.invoke(app, ["project", "init", "Demo", "--target", "Keep context bounded", "--id", "ctx", "--workdir", str(state)]).exit_code == 0
    assert runner.invoke(app, ["project", "bootstrap", "ctx", "--workdir", str(state)]).exit_code == 0
    result = runner.invoke(app, ["project", "context", "ctx", "--workdir", str(state)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["target"] == "Keep context bounded"
    assert "sessions" not in payload


def test_project_cli_exposes_team_runtime_commands(tmp_path: Path) -> None:
    result = runner.invoke(app, ["project", "--help"])
    assert result.exit_code == 0, result.stdout
    for command in ["run-next", "run", "finding", "resolve-finding", "artifact"]:
        assert command in result.stdout


def test_project_cli_records_findings_and_artifacts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert runner.invoke(app, ["project", "init", "Demo", "--target", "Track durable records", "--id", "records", "--workdir", str(state)]).exit_code == 0
    finding = runner.invoke(app, [
        "project", "finding", "records", "--claim", "Material issue", "--impact", "Blocks release", "--action", "Fix it", "--workdir", str(state),
    ])
    assert finding.exit_code == 0, finding.stdout
    finding_id = json.loads(finding.stdout)["finding_id"]
    artifact = runner.invoke(app, [
        "project", "artifact", "records", "--path", "report.md", "--kind", "report", "--workdir", str(state),
    ])
    assert artifact.exit_code == 0, artifact.stdout
    resolved = runner.invoke(app, [
        "project", "resolve-finding", "records", finding_id, "--resolution", "Fixed", "--workdir", str(state),
    ])
    assert resolved.exit_code == 0, resolved.stdout
    status = json.loads(runner.invoke(app, ["project", "status", "records", "--workdir", str(state)]).stdout)
    assert status["findings"][0]["status"] == "resolved"
    assert status["artifacts"][0]["path"] == "report.md"


def test_project_cli_submit_structured_external_outcome(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert runner.invoke(app, ["project", "init", "Demo", "--target", "External agent handoff", "--id", "handoff", "--workdir", str(state)]).exit_code == 0
    # Create an external implementer directly through the service so CLI is only testing the handoff surface.
    from fancy_gpt.project_models import AgentRole, WorkExecutionMode
    from fancy_gpt.project_service import ProjectService
    service = ProjectService(state)
    work = service.add_work_item("handoff", title="Implement", objective="Change source", role=AgentRole.IMPLEMENTER, execution_mode=WorkExecutionMode.EXTERNAL_AGENT)
    assignment = service.start_assignment("handoff", work.work_item_id)
    payload = {
        "request_id": "codex-local-1",
        "session_id": assignment.session.session_id,
        "work_item_id": work.work_item_id,
        "role": "implementer",
        "status": "complete",
        "summary": "Source and tests updated.",
        "decisions": [],
        "evidence": [{"claim": "Tests pass", "source": "pytest", "locator": "local"}],
        "findings": [],
        "artifacts": [{"path": "src/example.py", "kind": "source", "description": "implementation"}],
        "criterion_assessments": [],
        "next_actions": [],
        "confidence": 0.95,
        "relevance_assessment": {"within_requested_scope": True, "necessary_expansions": [], "omitted_non_material_topics": []},
        "conversation_binding": None,
    }
    outcome_file = tmp_path / "outcome.json"
    outcome_file.write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["project", "submit-outcome", "handoff", str(outcome_file), "--workdir", str(state)])
    assert result.exit_code == 0, result.stdout
    snapshot = service.snapshot("handoff")
    assert snapshot.work_item(work.work_item_id).state.value == "done"
    assert snapshot.evidence[0].claim == "Tests pass"
    assert snapshot.artifacts[0].path == "src/example.py"
