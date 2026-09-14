from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fancy_gpt.patcher import CommandRunner, PatchRejected, RepositoryPatcher
from fancy_gpt.project_models import (
    AgentOutcome,
    AgentOutcomeStatus,
    AgentRole,
    CodeChangeDraft,
    FileEditDraft,
)
from fancy_gpt.project_service import ProjectService


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ALPHA = "def alpha():\n    return 1\n"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "alpha.py").write_text(ALPHA, encoding="utf-8")
    return repo


def edit(**kw) -> FileEditDraft:
    kw.setdefault("rationale", "work item requires it")
    return FileEditDraft(**kw)


# --------------------------- patcher: what it accepts ---------------------------

def test_unique_replacement_is_applied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    RepositoryPatcher(repo).apply([
        edit(path="src/alpha.py", base_sha256=sha(ALPHA), old="return 1", new="return 2")
    ])
    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == "def alpha():\n    return 2\n"


def test_new_file_needs_no_base_hash(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = RepositoryPatcher(repo).apply([edit(path="src/beta.py", content="def beta():\n    return 2\n")])
    assert (repo / "src" / "beta.py").is_file()
    assert result.applied[0].created is True


# --------------------------- patcher: what it refuses ---------------------------

def test_mangled_whitespace_is_refused_rather_than_corrupting_the_file(tmp_path: Path) -> None:
    # The response is scraped out of the ChatGPT DOM, where soft wrapping can
    # inject a newline inside a string. In source code that would silently break
    # indentation, so an inexact match must fail closed.
    repo = _repo(tmp_path)
    with pytest.raises(PatchRejected, match="does not match the file"):
        RepositoryPatcher(repo).apply([
            # indentation flattened in transit, as a soft-wrapped scrape would do
            edit(path="src/alpha.py", base_sha256=sha(ALPHA), old="def alpha():\nreturn 1", new="def alpha():\nreturn 2")
        ])
    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == ALPHA


def test_ambiguous_replacement_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "twice.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(PatchRejected, match="occurs 2 times"):
        RepositoryPatcher(repo).apply([
            edit(path="src/twice.py", base_sha256=sha("x = 1\nx = 1\n"), old="x = 1", new="x = 2")
        ])


def test_stale_base_hash_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(PatchRejected, match="changed since it was supplied"):
        RepositoryPatcher(repo).apply([
            edit(path="src/alpha.py", base_sha256=sha("something else"), old="return 1", new="return 2")
        ])
    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == ALPHA


def test_path_escaping_the_repository_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    with pytest.raises(PatchRejected, match="repository-relative"):
        RepositoryPatcher(repo).apply([edit(path="../outside.py", content="pwned = True\n")])
    assert not outside.exists()


def test_secret_bearing_path_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(PatchRejected, match="secret-bearing"):
        RepositoryPatcher(repo).apply([edit(path=".env", content="KEY=value\n")])


def test_whole_file_replacement_of_an_existing_file_requires_a_base_hash(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(PatchRejected, match="requires base_sha256"):
        RepositoryPatcher(repo).apply([edit(path="src/alpha.py", content="wiped = True\n")])
    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == ALPHA


def test_a_change_is_all_or_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(PatchRejected):
        RepositoryPatcher(repo).apply([
            edit(path="src/good.py", content="ok = True\n"),
            edit(path="src/alpha.py", base_sha256=sha(ALPHA), old="never appears", new="x"),
        ])
    # The valid edit in the same change must not have been written.
    assert not (repo / "src" / "good.py").exists()


# --------------------------- command receipts ---------------------------

def test_runner_refuses_a_check_the_operator_did_not_allow(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = CommandRunner(repo, allowed={"unit": ["python", "-c", "print(1)"]})
    with pytest.raises(PatchRejected, match="unknown verification check"):
        runner.run("rm-rf")


def test_receipt_records_the_real_exit_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = CommandRunner(repo, allowed={
        "pass": ["python", "-c", "print('fine')"],
        "fail": ["python", "-c", "raise SystemExit(3)"],
    })
    ok = runner.run("pass")
    assert ok.ok and ok.exit_code == 0 and "fine" in ok.output_tail

    bad = runner.run("fail")
    assert not bad.ok and bad.exit_code == 3


# --------------------------- end to end through the service ---------------------------

def _outcome(service: ProjectService, project_id: str, item_id: str, change: CodeChangeDraft):
    assignment = service.start_assignment(project_id, item_id)
    return assignment, AgentOutcome(
        request_id="req-1",
        session_id=assignment.session.session_id,
        work_item_id=item_id,
        role=AgentRole.IMPLEMENTER,
        status=AgentOutcomeStatus.COMPLETE,
        summary="applied the planned change",
        confidence=0.9,
        code_change=change,
    )


def test_model_implementer_change_is_applied_and_verified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = ProjectService(
        tmp_path / "work",
        allowed_roots=[repo],
        verification_checks={"unit": ["python", "-c", "import sys; sys.exit(0)"]},
    )
    project = service.create_project(name="p", target="ship it", repo_root=str(repo))
    item = service.add_work_item(
        project.project_id, title="Implement", objective="bump the return value", role=AgentRole.IMPLEMENTER
    )
    change = CodeChangeDraft(
        summary="return 2 instead of 1",
        edits=[edit(path="src/alpha.py", base_sha256=sha(ALPHA), old="return 1", new="return 2")],
        verification_checks=["unit"],
    )
    _, outcome = _outcome(service, project.project_id, item.work_item_id, change)
    service.apply_agent_outcome(project.project_id, outcome)

    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == "def alpha():\n    return 2\n"
    snapshot = service.snapshot(project.project_id)
    assert [r.check for r in snapshot.receipts] == ["unit"]
    assert snapshot.receipts[0].exit_code == 0
    # The artifact records the hash of what is now actually on disk.
    artifact = next(a for a in snapshot.artifacts if a.path == "src/alpha.py")
    assert artifact.sha256 == sha("def alpha():\n    return 2\n")


def test_a_refused_change_fails_the_session_instead_of_passing_silently(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = ProjectService(tmp_path / "work", allowed_roots=[repo])
    project = service.create_project(name="p", target="ship it", repo_root=str(repo))
    item = service.add_work_item(
        project.project_id, title="Implement", objective="bad edit", role=AgentRole.IMPLEMENTER
    )
    change = CodeChangeDraft(
        summary="edit that cannot match",
        edits=[edit(path="src/alpha.py", base_sha256=sha(ALPHA), old="not present", new="x")],
    )
    assignment, outcome = _outcome(service, project.project_id, item.work_item_id, change)
    outcome = AgentOutcome.model_validate({
        **outcome.model_dump(),
        "decisions": [{"statement": "Ship the refused edit", "rationale": "exercise atomicity"}],
        "evidence": [{"claim": "The refused edit works", "source": "failed teammate"}],
    })
    with pytest.raises(PatchRejected):
        service.apply_agent_outcome(project.project_id, outcome)

    assert (repo / "src" / "alpha.py").read_text(encoding="utf-8") == ALPHA
    session = service.session(project.project_id, assignment.session.session_id)
    assert session.state.value == "failed"
    snapshot = service.snapshot(project.project_id)
    assert snapshot.decisions == []
    assert snapshot.evidence == []


def test_implementer_prompt_carries_the_code_change_contract(tmp_path: Path) -> None:
    from fancy_gpt.team_agent import TeamAgentEngine

    repo = _repo(tmp_path)
    service = ProjectService(tmp_path / "work", allowed_roots=[repo], verification_checks={"unit": ["true"]})
    project = service.create_project(name="p", target="ship it", repo_root=str(repo))
    item = service.add_work_item(
        project.project_id, title="Implement", objective="change code", role=AgentRole.IMPLEMENTER
    )
    assignment = service.start_assignment(project.project_id, item.work_item_id)
    prompt = TeamAgentEngine().build_request(assignment, request_id="r1").prompt
    assert "CODE CHANGE CONTRACT" in prompt
    assert "EXACTLY ONCE" in prompt
    assert "unit" in prompt

    # A role that must not touch the repository is not invited to propose edits.
    reviewer = service.add_work_item(
        project.project_id, title="Review", objective="review", role=AgentRole.REVIEWER
    )
    reviewer_prompt = TeamAgentEngine().build_request(
        service.start_assignment(project.project_id, reviewer.work_item_id), request_id="r2"
    ).prompt
    assert "CODE CHANGE CONTRACT" not in reviewer_prompt


def test_blocks_survive_the_browser_render_that_strips_the_fences(tmp_path: Path) -> None:
    # The response is scraped from the rendered page, where a fence has become a
    # <pre> element: the backticks are gone and only the info string is left as a
    # bare line. Indentation, which is what actually matters, is preserved.
    from fancy_gpt.response_parser import parse_json_object_with_blocks

    scraped = (
        "JSON\n"
        '{"role": "implementer", "edit": {"old_ref": "E1-OLD", "new_ref": "E1-NEW"}}\n'
        "fancygpt:E1-OLD\n"
        '            "version_contract": __version__,\n'
        "fancygpt:E1-NEW\n"
        '            "version_contract": __version__,\n'
        '            "team_code_change": True,\n'
    )
    payload, blocks = parse_json_object_with_blocks(scraped)
    assert payload["role"] == "implementer"
    assert blocks["E1-OLD"] == '            "version_contract": __version__,'
    assert blocks["E1-NEW"].startswith('            "version_contract"')
    assert '            "team_code_change": True,' in blocks["E1-NEW"]


def test_code_in_a_block_needs_no_json_escaping(tmp_path: Path) -> None:
    # The failure that forced this design: the edited source contains quotes, so
    # embedding it in a JSON string requires escaping the model reliably gets wrong.
    from fancy_gpt.response_parser import parse_json_object_with_blocks

    payload, blocks = parse_json_object_with_blocks(
        '{"ok": true}\n'
        "fancygpt:E1\n"
        '    return {"ok": True, "path": "C:\\\\tmp"}\n'
    )
    assert payload == {"ok": True}
    assert blocks["E1"] == '    return {"ok": True, "path": "C:\\\\tmp"}'
