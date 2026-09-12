from pathlib import Path
import json

import pytest

from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.models import RawRequest
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from tests.helpers import final_payload, planner_payload


def make_repo(tmp_path: Path) -> Path:
    repo=tmp_path/"repo"; (repo/"docs").mkdir(parents=True); (repo/"docs"/"design.md").write_text("# Design\nA -> B\n")
    return repo


def test_full_interactive_state_machine(tmp_path: Path):
    repo=make_repo(tmp_path); work=tmp_path/"work"
    engine=ReviewEngine(work,allowed_roots=[tmp_path])
    req=RawRequest(mode="review",objective="review architecture boundaries",repo_root=str(repo),domains=["architecture"])
    planner=engine.prepare(req,skill_name="technical-review")
    route=engine.route(req,skill_name="technical-review")
    pp=planner_payload(required_sections=route.required_sections)
    final=engine.submit_planner(planner.request_id,pp)
    result=engine.submit_final(planner.request_id,final_payload(planner.request_id,sections=route.required_sections))
    assert final.stage=="final"
    assert result.confidence==0.8
    assert engine.status(planner.request_id).state.value=="complete"
    assert engine.inspect_context(planner.request_id).artifacts[0].path=="docs/design.md"


def test_full_automatic_two_pass_workflow(tmp_path: Path):
    repo=make_repo(tmp_path); engine=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path])
    req=RawRequest(mode="review",objective="review architecture",repo_root=str(repo),domains=["architecture"])
    route=engine.route(req,skill_name="technical-review")
    driver=FakeBrowserDriver([
        json.dumps(planner_payload(required_sections=route.required_sections)),
        lambda turn: json.dumps(final_payload(turn.request_id,sections=route.required_sections)),
    ])
    report=engine.run_automatic(req,ChatGPTWebAutomationProvider(driver,timeout_s=5),skill_name="technical-review")
    assert report.status=="complete"
    assert [stage for _,stage,_ in driver.prompts]==["planner","final"]


def test_report_must_cover_research_trace_and_sections(tmp_path: Path):
    repo=make_repo(tmp_path); engine=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path])
    req=RawRequest(mode="review",objective="review",repo_root=str(repo),domains=["architecture"])
    inter=engine.prepare(req,skill_name="technical-review"); route=engine.route(req,skill_name="technical-review")
    engine.submit_planner(inter.request_id,planner_payload(required_sections=route.required_sections))
    bad=final_payload(inter.request_id,sections=route.required_sections); bad["research_trace"]=[]
    with pytest.raises(ValueError,match="online research tasks"): engine.submit_final(inter.request_id,bad)


def test_allowed_root_is_checked_before_online_prompt(tmp_path: Path):
    allowed=tmp_path/"allowed"; allowed.mkdir(); outside=tmp_path/"outside"; outside.mkdir()
    engine=ReviewEngine(tmp_path/"work",allowed_roots=[allowed])
    req=RawRequest(mode="review",objective="review",repo_root=str(outside))
    with pytest.raises(Exception,match="outside configured allowed roots"):
        engine.prepare(req,skill_name="technical-review")


def test_final_report_requires_validation_plan_when_manifest_contract_requires_it(tmp_path: Path):
    from fancy_gpt.models import RawRequest, ResearchManifest
    from tests.helpers import planner_payload, final_payload
    engine = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path])
    request = RawRequest(mode="review", objective="review", repo_root=str(tmp_path), domains=["architecture"])
    route = engine.route(request, skill_name="technical-review")
    manifest = ResearchManifest.model_validate(planner_payload(required_sections=route.required_sections))
    payload = final_payload("rid", sections=route.required_sections)
    payload["validation_plan"] = []
    with pytest.raises(ValueError, match="validation plan"):
        engine._validate_report("rid", request, route, manifest, payload)


def test_final_report_rejects_unjustified_scope_expansion(tmp_path: Path):
    from fancy_gpt.models import RawRequest, ResearchManifest
    from tests.helpers import planner_payload, final_payload
    engine = ReviewEngine(tmp_path / "work", allowed_roots=[tmp_path])
    request = RawRequest(mode="review", objective="review", repo_root=str(tmp_path), domains=["architecture"])
    route = engine.route(request, skill_name="technical-review")
    manifest = ResearchManifest.model_validate(planner_payload(required_sections=route.required_sections))
    payload = final_payload("rid", sections=route.required_sections)
    payload["relevance_assessment"] = {"within_requested_scope": False, "necessary_expansions": [], "omitted_non_material_topics": []}
    with pytest.raises(ValueError, match="justify material scope expansion"):
        engine._validate_report("rid", request, route, manifest, payload)
