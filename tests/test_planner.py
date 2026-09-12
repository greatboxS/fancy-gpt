from pathlib import Path

import pytest

from fancy_gpt.engine import ReviewEngine
from fancy_gpt.models import ArtifactRole, ArtifactSpec, RawRequest, ResearchManifest
from fancy_gpt.planner import PreRequestPlanner
from tests.helpers import planner_payload


def test_planner_is_online_research_planner_not_solver(tmp_path: Path):
    req=RawRequest(mode="review",objective="review architecture",repo_root=str(tmp_path),domains=["architecture"],artifacts=[ArtifactSpec(name="secret-design",content="BODY MUST NOT ENTER PLANNER")])
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,skill_name="technical-review")
    prompt=PreRequestPlanner().build_prompt("abc",req,route)
    assert "Do not solve the engineering problem" in prompt
    assert "BODY MUST NOT ENTER PLANNER" not in prompt
    assert "domain_policies" in prompt
    assert "available_capabilities" in prompt
    assert "count them before returning" in prompt
    assert 'exact_paths: ["<git-diff>"]' in prompt


def test_design_candidate_is_absent_even_as_metadata(tmp_path: Path):
    req=RawRequest(mode="design",objective="design system",repo_root=str(tmp_path),domains=["architecture"],include=["secret-candidate/**/*.py"],exclude=["secret-candidate/legacy.py"],notes="candidate uses a revealing implementation",artifacts=[ArtifactSpec(path="candidate.md",role=ArtifactRole.CANDIDATE_SOLUTION)])
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,skill_name="independent-design")
    prompt=PreRequestPlanner().build_prompt("abc",req,route)
    assert "candidate.md" not in prompt
    assert "secret-candidate" not in prompt
    assert "revealing implementation" not in prompt


def test_planner_rejects_unrouted_capability_and_missing_sections(tmp_path: Path):
    req=RawRequest(mode="review",objective="review",repo_root=str(tmp_path),domains=["documentation"],freshness="static")
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,skill_name="technical-review")
    payload=planner_payload(required_sections=route.required_sections)
    payload["tool_plan"]=[{"capability":"web.search","purpose":"not routed","required":False}]
    manifest=ResearchManifest.model_validate(payload)
    with pytest.raises(ValueError,match="unavailable capability"):
        PreRequestPlanner().validate_manifest(manifest,route)


def test_planner_rejects_required_context_without_machine_selectors(tmp_path: Path):
    req = RawRequest(mode="review", objective="review", repo_root=str(tmp_path), domains=["architecture"])
    route = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path]).route(req, skill_name="technical-review")
    payload = planner_payload(required_sections=route.required_sections)
    payload["local_context_requirements"][0].update(patterns=[], exact_paths=[], search_terms=[])
    manifest = ResearchManifest.model_validate(payload)
    with pytest.raises(ValueError, match="actionable selectors"):
        PreRequestPlanner().validate_manifest(manifest, route)


def test_final_prompt_does_not_reintroduce_candidate_or_host_path(tmp_path: Path):
    from fancy_gpt.context_builder import ContextBuilder
    from fancy_gpt.models import ArtifactSpec, ArtifactRole
    from fancy_gpt.prompt_compiler import FinalPromptCompiler
    p=planner_payload(mode="design",required_sections=["executive-summary","design","alternatives","risks","validation-plan"])
    p["online_research"]=[]; p["tool_plan"]=[]; p["local_context_requirements"]=[]
    req=RawRequest(mode="design",objective="design cleanly",repo_root=str(tmp_path),domains=["architecture"],include=["candidate-secret-dir/**"],notes="CANDIDATE NOTE",artifacts=[ArtifactSpec(name="candidate-secret-name",content="CANDIDATE BODY",role=ArtifactRole.CANDIDATE_SOLUTION),ArtifactSpec(name="requirements",content="REQ-1",role=ArtifactRole.REQUIREMENT)])
    engine=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path])
    route=engine.route(req,skill_name="independent-design")
    manifest=ResearchManifest.model_validate(p)
    context=ContextBuilder().build(req,manifest)
    prompt=FinalPromptCompiler().build_prompt("r",req,route,manifest,context)
    assert "CANDIDATE BODY" not in prompt
    assert "candidate-secret-name" not in prompt
    assert "candidate-secret-dir" not in prompt
    assert "CANDIDATE NOTE" not in prompt
    assert str(tmp_path) not in prompt
    assert "REQ-1" in prompt


def test_design_sanitizer_removes_candidate_navigation_metadata(tmp_path: Path):
    marker = "candidate-super-secret-module"
    req = RawRequest(
        mode="design",
        objective="design a replacement from requirements",
        repo_root=str(tmp_path),
        domains=["architecture"],
        include=[f"src/{marker}.cpp"],
        exclude=[f"legacy/{marker}/**"],
        notes=f"Current candidate uses {marker}; do not anchor on it.",
    )
    route = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path]).route(req, skill_name="independent-design")
    prompt = PreRequestPlanner().build_prompt("abc", req, route)
    assert marker not in prompt
    assert str(tmp_path) not in prompt
