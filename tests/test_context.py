from pathlib import Path
import subprocess

import pytest

from fancy_gpt.context_builder import ContextBuilder
from fancy_gpt.errors import ContextRequirementError, ContextSecurityError
from fancy_gpt.models import ArtifactRole, ArtifactSpec, RawRequest, ResearchManifest
from tests.helpers import planner_payload


def manifest(pattern="src/**/*.cpp", required=True):
    p=planner_payload()
    p["online_research"]=[]; p["tool_plan"]=[{"capability":"repo.read","purpose":"read","required":True}]
    p["local_context_requirements"]=[{"id":"LC1","description":"source","patterns":[pattern],"exact_paths":[],"search_terms":[],"priority":"P0","required":required}]
    return ResearchManifest.model_validate(p)


def test_context_pack_hash_required_gate_and_redacted_root(tmp_path: Path):
    (tmp_path/"src").mkdir(); (tmp_path/"src"/"a.cpp").write_text("int x=1;\n")
    pack=ContextBuilder().build(RawRequest(mode="review",objective="review code",repo_root=str(tmp_path)),manifest())
    assert pack.repo_root=="<local-repo-root>"
    assert pack.satisfied_requirements==["LC1"]
    assert pack.artifacts[0].path=="src/a.cpp"
    assert len(pack.context_hash)==64


def test_broad_scan_skips_symlink_and_explicit_escape_fails(tmp_path: Path):
    outside=tmp_path.parent/"outside.txt"; outside.write_text("secret")
    (tmp_path/"leak.txt").symlink_to(outside)
    p=planner_payload(); p["online_research"]=[]; p["tool_plan"]=[]
    p["local_context_requirements"]=[{"id":"LC","description":"search","patterns":[],"exact_paths":[],"search_terms":["secret"],"priority":"P1","required":False}]
    pack=ContextBuilder().build(RawRequest(mode="review",objective="scan",repo_root=str(tmp_path)),ResearchManifest.model_validate(p))
    assert not pack.artifacts
    req=RawRequest(mode="review",objective="escape",repo_root=str(tmp_path),include=["../outside.txt"])
    with pytest.raises(ContextSecurityError): ContextBuilder().build(req,ResearchManifest.model_validate(p))


def test_independent_design_filters_candidate_and_git_diff(tmp_path: Path):
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    subprocess.run(["git","-C",str(tmp_path),"config","user.email","x@y"],check=True)
    subprocess.run(["git","-C",str(tmp_path),"config","user.name","T"],check=True)
    (tmp_path/"req.md").write_text("REQ")
    (tmp_path/"candidate.md").write_text("CANDIDATE SECRET")
    subprocess.run(["git","-C",str(tmp_path),"add","."],check=True); subprocess.run(["git","-C",str(tmp_path),"commit","-qm","init"],check=True)
    (tmp_path/"candidate.md").write_text("CANDIDATE SECRET CHANGED")
    p=planner_payload(mode="design",required_sections=["executive-summary","design","alternatives","risks","validation-plan"])
    p["online_research"]=[]; p["tool_plan"]=[]; p["local_context_requirements"]=[]
    req=RawRequest(mode="design",objective="design",repo_root=str(tmp_path),include_git_diff=True,artifacts=[ArtifactSpec(path="req.md",role=ArtifactRole.REQUIREMENT),ArtifactSpec(path="candidate.md",role=ArtifactRole.CANDIDATE_SOLUTION)])
    pack=ContextBuilder().build(req,ResearchManifest.model_validate(p))
    text="\n".join(a.content for a in pack.artifacts)
    assert "REQ" in text
    assert "CANDIDATE SECRET" not in text
    assert all(a.source!="git-diff" for a in pack.artifacts)


def test_required_context_fails_after_filter(tmp_path: Path):
    (tmp_path/"candidate.md").write_text("candidate")
    p=planner_payload(mode="design",required_sections=["executive-summary","design","alternatives","risks","validation-plan"])
    p["online_research"]=[]; p["tool_plan"]=[]
    p["local_context_requirements"]=[{"id":"LC1","description":"candidate","patterns":["candidate.md"],"exact_paths":[],"search_terms":[],"priority":"P0","required":True}]
    req=RawRequest(mode="design",objective="design",repo_root=str(tmp_path),artifacts=[ArtifactSpec(path="candidate.md",role=ArtifactRole.CANDIDATE_SOLUTION)])
    with pytest.raises(ContextRequirementError): ContextBuilder().build(req,ResearchManifest.model_validate(p))


def test_root_anchored_slash_patterns_are_treated_as_repo_relative(tmp_path: Path):
    # Reproduces a real fancy-gpt failure: the planner emitted a gitignore-style
    # root-anchored pattern ("/CMakeLists.txt") which used to be misread as a
    # filesystem-absolute path and rejected outright, crashing the whole run.
    (tmp_path / "CMakeLists.txt").write_text("project(x)\n")
    pack = ContextBuilder().build(
        RawRequest(mode="review", objective="review build", repo_root=str(tmp_path)),
        manifest(pattern="/CMakeLists.txt"),
    )
    assert pack.artifacts[0].path == "CMakeLists.txt"


def test_root_anchored_slash_exact_path_is_treated_as_repo_relative(tmp_path: Path):
    (tmp_path / "CMakeLists.txt").write_text("project(x)\n")
    p = planner_payload()
    p["online_research"] = []
    p["tool_plan"] = [{"capability": "repo.read", "purpose": "read", "required": True}]
    p["local_context_requirements"] = [{
        "id": "LC1", "description": "build file", "patterns": [], "exact_paths": ["/CMakeLists.txt"],
        "search_terms": [], "priority": "P0", "required": True,
    }]
    pack = ContextBuilder().build(
        RawRequest(mode="review", objective="review build", repo_root=str(tmp_path)),
        ResearchManifest.model_validate(p),
    )
    assert pack.artifacts[0].path == "CMakeLists.txt"


def test_secret_policy_denies_secret_files_and_redacts_high_confidence_tokens(tmp_path: Path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.txt").write_text("token=ghp_abcdefghijklmnopqrstuvwxyz123456\nnormal=value\n")
    p = planner_payload()
    p["online_research"] = []
    p["tool_plan"] = []
    p["local_context_requirements"] = [{
        "id": "LC", "description": "config", "patterns": ["src/config.txt", ".env"],
        "exact_paths": [], "search_terms": [], "priority": "P1", "required": False,
    }]
    pack = ContextBuilder().build(
        RawRequest(mode="review", objective="review config", repo_root=str(tmp_path)),
        ResearchManifest.model_validate(p),
    )
    assert all(item.path != ".env" for item in pack.artifacts)
    assert any("secret-policy:.env" in item for item in pack.omitted)
    config = next(item for item in pack.artifacts if item.path == "src/config.txt")
    assert "ghp_" not in config.content
    assert "<redacted:github-token>" in config.content
    assert "normal=value" in config.content


def test_required_p0_secret_context_fails_closed(tmp_path: Path):
    (tmp_path / "private.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    p = planner_payload()
    p["online_research"] = []
    p["tool_plan"] = []
    p["local_context_requirements"] = [{
        "id": "LC", "description": "private key", "patterns": [],
        "exact_paths": ["private.pem"], "search_terms": [], "priority": "P0", "required": True,
    }]
    with pytest.raises(ContextRequirementError, match="secret policy"):
        ContextBuilder().build(
            RawRequest(mode="review", objective="inspect key", repo_root=str(tmp_path)),
            ResearchManifest.model_validate(p),
        )


def test_search_acquisition_budget_fails_closed_for_required_context(tmp_path: Path):
    (tmp_path / "src").mkdir()
    for i in range(4):
        (tmp_path / "src" / f"f{i}.txt").write_text("needle " + ("x" * 50))
    p = planner_payload()
    p["online_research"] = []
    p["tool_plan"] = []
    p["local_context_requirements"] = [{
        "id": "LC", "description": "search", "patterns": [], "exact_paths": [],
        "search_terms": ["needle"], "priority": "P0", "required": True,
    }]
    with pytest.raises(ContextRequirementError, match="acquisition budget"):
        ContextBuilder(max_scan_files=1).build(
            RawRequest(mode="review", objective="find needle", repo_root=str(tmp_path)),
            ResearchManifest.model_validate(p),
        )
