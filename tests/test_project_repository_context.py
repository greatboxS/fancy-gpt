from __future__ import annotations

from pathlib import Path

import pytest

from fancy_gpt.context_builder import ContextBuilder, ContextSecurityError
from fancy_gpt.models import LocalContextRequirement
from fancy_gpt.project_models import AgentRole
from fancy_gpt.project_service import ProjectService


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    return repo


def _service(tmp_path: Path, repo: Path) -> ProjectService:
    return ProjectService(tmp_path / "work", allowed_roots=[repo])


def _project(service: ProjectService, repo: Path):
    return service.create_project(name="p", target="ship it", repo_root=str(repo))


def test_exact_paths_and_globs_reach_the_assignment(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = _service(tmp_path, repo)
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Read source",
        objective="inspect the module",
        role=AgentRole.RESEARCHER,
        context_requirements=[
            LocalContextRequirement(id="LC1", description="alpha", exact_paths=["src/alpha.py"]),
            LocalContextRequirement(id="LC2", description="readme", patterns=["*.md"]),
        ],
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    supplied = {a.path: a for a in assignment.relevant_context.repository_source}

    assert "src/alpha.py" in supplied
    assert "README.md" in supplied
    assert "def alpha()" in supplied["src/alpha.py"].content
    # Nothing that was not asked for.
    assert "src/beta.py" not in supplied


def test_work_item_without_requirements_reads_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    class ExplodingBuilder(ContextBuilder):
        def build(self, request, manifest):  # pragma: no cover - must never run
            raise AssertionError("repository was read for a work item that declared no context")

    service = ProjectService(tmp_path / "work", context_builder=ExplodingBuilder(), allowed_roots=[repo])
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id, title="No source", objective="think only", role=AgentRole.DESIGNER
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    assert assignment.relevant_context.repository_source == []


def test_requirement_without_any_selector_still_reads_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    class ExplodingBuilder(ContextBuilder):
        def build(self, request, manifest):  # pragma: no cover - must never run
            raise AssertionError("repository was read for a requirement with no selector")

    service = ProjectService(tmp_path / "work", context_builder=ExplodingBuilder(), allowed_roots=[repo])
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Empty selector",
        objective="think only",
        role=AgentRole.DESIGNER,
        context_requirements=[LocalContextRequirement(id="LC", description="nothing")],
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    assert assignment.relevant_context.repository_source == []


def test_repo_root_outside_allowed_roots_is_rejected_before_any_read(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    service = ProjectService(tmp_path / "work", allowed_roots=[elsewhere])
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Read source",
        objective="inspect",
        role=AgentRole.RESEARCHER,
        context_requirements=[LocalContextRequirement(id="LC", description="alpha", exact_paths=["src/alpha.py"])],
    )
    with pytest.raises(ContextSecurityError, match="allowed roots"):
        service.start_assignment(project.project_id, item.work_item_id)


def test_secret_policy_applies_to_assignment_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".env").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")
    (repo / "src" / "config.py").write_text(
        'TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz123456"\nKEEP = "value"\n', encoding="utf-8"
    )
    service = _service(tmp_path, repo)
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Read config",
        objective="inspect config",
        role=AgentRole.RESEARCHER,
        context_requirements=[
            LocalContextRequirement(id="LC", description="config", patterns=["src/config.py", ".env"])
        ],
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    context = assignment.relevant_context
    supplied = {a.path: a for a in context.repository_source}

    assert ".env" not in supplied
    assert any("secret-policy:.env" in item for item in context.repository_omitted)
    assert "ghp_" not in supplied["src/config.py"].content
    assert "<redacted:github-token>" in supplied["src/config.py"].content
    assert 'KEEP = "value"' in supplied["src/config.py"].content


def test_supplied_artifact_hash_matches_the_content_actually_delivered(tmp_path: Path) -> None:
    import hashlib

    repo = _repo(tmp_path)
    service = _service(tmp_path, repo)
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Cite source",
        objective="cite a real file",
        role=AgentRole.RESEARCHER,
        context_requirements=[LocalContextRequirement(id="LC", description="alpha", exact_paths=["src/alpha.py"])],
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    artifact = next(a for a in assignment.relevant_context.repository_source if a.path == "src/alpha.py")

    # A teammate citing path + sha256 can be checked against what it was given.
    assert artifact.sha256 == hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()

    evidence = service.record_evidence(
        project.project_id,
        claim="alpha() returns 1",
        source=artifact.path,
        locator=artifact.sha256,
        source_session_id=assignment.session.session_id,
    )
    stored = next(e for e in service.snapshot(project.project_id).evidence if e.evidence_id == evidence.evidence_id)
    assert stored.source == "src/alpha.py"
    assert stored.locator == artifact.sha256


def test_assignment_prompt_carries_the_supplied_source_and_provenance_rule(tmp_path: Path) -> None:
    from fancy_gpt.team_agent import TeamAgentEngine

    repo = _repo(tmp_path)
    service = _service(tmp_path, repo)
    project = _project(service, repo)
    item = service.add_work_item(
        project.project_id,
        title="Read source",
        objective="inspect the module",
        role=AgentRole.RESEARCHER,
        context_requirements=[LocalContextRequirement(id="LC", description="alpha", exact_paths=["src/alpha.py"])],
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    prompt = TeamAgentEngine().build_request(assignment, request_id="req-1").prompt

    assert "def alpha()" in prompt
    assert "repository_source" in prompt
