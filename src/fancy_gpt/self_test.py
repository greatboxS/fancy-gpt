from __future__ import annotations

from . import __version__

import json
import tempfile
from pathlib import Path

from .browser import FakeBrowserDriver
from .catalog import load_domains, load_skills, load_workflows
from .engine import ReviewEngine
from .models import RawRequest
from .providers import ChatGPTWebAutomationProvider
from .orchestrator import TeamOrchestrator
from .project_models import AcceptanceCriterion, AgentRole, CriterionStatus
from .project_service import ProjectService
from .skills import packaged_skills_root, validate_skill_bundle
from .tunnels import TunnelLayerInspector, TunnelRegistry


def _planner_payload() -> dict:
    return {
        "intent": ["review"],
        "domains": ["architecture"],
        "tags": ["self-test"],
        "research_goal": "Validate the local two-pass runtime contract.",
        "research_questions": [
            {"id": "Q1", "question": "Does the sample artifact satisfy the stated invariant?", "rationale": "Exercises planner to final flow.", "priority": "P0"}
        ],
        "local_context_requirements": [
            {"id": "L1", "description": "Read the sample artifact", "exact_paths": ["sample.txt"], "priority": "P0", "required": True}
        ],
        "online_research": [],
        "source_map": [],
        "evidence_requirements": [],
        "tool_plan": [{"capability": "repo.read", "purpose": "Read local sample artifact", "required": True}],
        "freshness": "static",
        "missing_evidence": [],
        "report_contract": {
            "sections": ["executive-summary", "findings", "recommendation", "unknowns", "validation-plan"],
            "require_citations": False,
            "require_evidence_mapping": False,
            "require_unknowns": True,
            "require_validation_plan": True,
            "style": "concise, technical, evidence-first"
        },
        "research_budget": {"max_search_queries": 0, "max_open_pages": 0, "max_context_bytes": 65536}
    }


def _final_payload(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "mode": "review",
        "status": "complete",
        "verdict": "pass",
        "executive_summary": "The offline two-pass runtime completed successfully.",
        "report_sections_completed": ["executive-summary", "findings", "recommendation", "unknowns", "validation-plan"],
        "findings": [],
        "options": [],
        "hypotheses": [],
        "deliverables": [],
        "recommendation": "Runtime contract is healthy.",
        "assumptions_challenged": [],
        "unknowns": [],
        "validation_plan": [{"step": "Run packaged self-test", "expected_evidence": "COMPLETE final report", "pass_condition": "No exception and status complete"}],
        "research_trace": [],
        "evidence_coverage": [],
        "sources": [],
        "confidence": 1.0
    }


def run_self_test() -> dict:
    """Run a package-only, offline, end-to-end smoke test.

    This intentionally does not require the source repository, pytest, Internet,
    a browser, ChatGPT credentials, Codex, or an OpenAI API key.
    """
    skills = load_skills()
    workflows = load_workflows()
    domains = load_domains()
    errors = validate_skill_bundle(packaged_skills_root())
    if errors:
        raise RuntimeError(f"packaged Agent Skills invalid: {errors}")
    if (len(skills), len(workflows), len(domains)) != (6, 4, 13):
        raise RuntimeError(f"catalog invariant failed: skills={len(skills)} workflows={len(workflows)} domains={len(domains)}")

    registry = TunnelRegistry()
    inspector = TunnelLayerInspector()
    tunnel_errors = {
        spec.id: [item.detail for item in inspector.inspect(spec) if item.state != "healthy"]
        for spec in registry.all()
    }
    tunnel_errors = {key: value for key, value in tunnel_errors.items() if value}
    if tunnel_errors:
        raise RuntimeError(f"tunnel layer contract failed: {tunnel_errors}")

    with tempfile.TemporaryDirectory(prefix="fancy-gpt-selftest-") as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        (repo / "sample.txt").write_text("invariant=healthy\n", encoding="utf-8")
        workdir = root / "state"
        request = RawRequest.model_validate({
            "mode": "review",
            "objective": "Self-test the installed fancy-gpt runtime.",
            "repo_root": str(repo),
            "domains": ["architecture"],
            "focus": ["runtime contract"],
            "include": ["sample.txt"],
            "freshness": "static",
            "max_context_bytes": 65536,
        })
        driver = FakeBrowserDriver([
            json.dumps(_planner_payload()),
            lambda turn: json.dumps(_final_payload(turn.request_id)),
        ])
        provider = ChatGPTWebAutomationProvider(driver)
        report = ReviewEngine(workdir, allowed_roots=[repo]).run_automatic(
            request,
            provider,
            skill_name="technical-review",
        )
        if report.status != "complete" or report.verdict != "pass":
            raise RuntimeError("offline automatic self-test did not complete")

        # v0.8 persistent-team vertical slice: create a target, execute one
        # required assignment, persist evidence, and prove that completion
        # requires BOTH work completion and evidence-backed acceptance.
        project_service = ProjectService(workdir)
        project = project_service.create_project(
            project_id="selftest-project",
            name="Self Test",
            target="Prove persistent team state",
            acceptance=[AcceptanceCriterion(id="ac-1", statement="Evidence persists", evidence_required=["recorded evidence"])],
        )
        work = project_service.add_work_item(
            project.project_id,
            title="Verify persistence",
            objective="Persist one durable engineering-team session",
            role=AgentRole.VERIFIER,
        )
        assignment = project_service.start_assignment(project.project_id, work.work_item_id)
        project_service.finish_session(project.project_id, assignment.session.session_id, summary="verification session persisted")
        evidence = project_service.record_evidence(project.project_id, claim="state reloaded", source="offline self-test")
        project_service.update_criterion(project.project_id, "ac-1", status=CriterionStatus.SATISFIED, evidence_ids=[evidence.evidence_id])
        reloaded = ProjectService(workdir).snapshot(project.project_id)
        if reloaded.project.status.value != "complete" or not reloaded.sessions:
            raise RuntimeError("persistent team self-test did not complete")
        return {
            "ok": True,
            "version_contract": __version__,
            "skills": len(skills),
            "workflows": len(workflows),
            "domains": len(domains),
            "tunnels": len(registry.all()),
            "tunnel_layers": "healthy",
            "offline_two_pass": "complete",
            "persistent_team_state": "complete",
            "semantic_relevance_policy": "enabled",
            "browser_required": False,
            "network_required": False,
        }
