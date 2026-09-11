from pathlib import Path

import pytest

from fancy_gpt.catalog import load_domains, load_skills, load_workflows
from fancy_gpt.engine import ReviewEngine
from fancy_gpt.models import RawRequest

EXPECTED_SKILLS={"technical-review","independent-design","technical-consult","root-cause-investigation","evidence-verification","technical-writing"}
EXPECTED_WORKFLOWS={"deep-design-review","change-impact-assessment","production-readiness","conformance-audit"}


def test_catalog_is_compact_and_complete():
    skills=load_skills(); workflows=load_workflows(); domains=load_domains()
    assert set(skills)==EXPECTED_SKILLS
    assert set(workflows)==EXPECTED_WORKFLOWS
    assert len(domains)==13
    assert {s.mode.value for s in skills.values()} == {"review","design","consult","investigate","verify","write"}
    assert all(d.functional_focus and d.source_priority for d in domains.values())
    assert all(w.primary_skill in skills for w in workflows.values())
    assert all(set(w.component_skills) <= set(skills) for w in workflows.values())


def test_skill_routing_keeps_domain_and_focus(tmp_path: Path):
    req=RawRequest(mode="review",objective="review qos",repo_root=str(tmp_path),domains=["network","yocto"],focus=["tc/nft bottleneck"])
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,skill_name="technical-review")
    assert route.route_kind=="skill"
    assert route.primary_skill=="technical-review"
    assert route.domains==["network","yocto"]
    assert "tc/nft bottleneck" in route.focus
    assert "web.search" in route.allowed_capabilities


def test_workflow_expands_focus_without_becoming_skill(tmp_path: Path):
    req=RawRequest(mode="verify",objective="production gate",repo_root=str(tmp_path))
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,workflow_name="production-readiness")
    assert route.route_kind=="workflow"
    assert route.primary_skill=="evidence-verification"
    assert "productization" in route.domains
    assert "production-readiness" not in load_skills()
    assert len(route.component_skills) >= 2


def test_exactly_one_route_is_required(tmp_path: Path):
    req=RawRequest(mode="review",objective="review",repo_root=str(tmp_path))
    engine=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path])
    with pytest.raises(ValueError): engine.route(req)
    with pytest.raises(ValueError): engine.route(req,skill_name="technical-review",workflow_name="deep-design-review")

@pytest.mark.parametrize("workflow,mode",[
    ("deep-design-review","review"),
    ("change-impact-assessment","verify"),
    ("production-readiness","verify"),
    ("conformance-audit","verify"),
])
def test_all_workflows_route_to_canonical_skills(tmp_path: Path, workflow: str, mode: str):
    req=RawRequest(mode=mode,objective="workflow check",repo_root=str(tmp_path),include_git_diff=(workflow=="change-impact-assessment"))
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,workflow_name=workflow)
    assert route.route_name==workflow
    assert route.primary_skill in EXPECTED_SKILLS
    assert set(route.component_skills) <= EXPECTED_SKILLS
    assert route.required_sections


def test_workflow_defaults_are_preserved_when_user_adds_domain(tmp_path: Path):
    req=RawRequest(mode="verify",objective="prod",repo_root=str(tmp_path),domains=["network"])
    route=ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,workflow_name="production-readiness")
    assert {"architecture","embedded-linux","security","testing","productization","network"} <= set(route.domains)


def test_change_impact_requires_change_scope(tmp_path: Path):
    req=RawRequest(mode="verify",objective="impact",repo_root=str(tmp_path))
    with pytest.raises(ValueError,match="requires git diff"):
        ReviewEngine(tmp_path/"work",allowed_roots=[tmp_path]).route(req,workflow_name="change-impact-assessment")
