from __future__ import annotations

from fancy_gpt.catalog import load_domains, load_skills, load_workflows
from fancy_gpt.models import RequestMode
from fancy_gpt.skills import packaged_skills_root, validate_skill_bundle

EXPECTED_SKILLS = {
    "technical-review", "independent-design", "technical-consult",
    "root-cause-investigation", "evidence-verification", "technical-writing",
}
EXPECTED_WORKFLOWS = {
    "deep-design-review", "change-impact-assessment", "production-readiness", "conformance-audit",
}

skills = load_skills(); workflows = load_workflows(); domains = load_domains()
assert set(skills) == EXPECTED_SKILLS
assert set(workflows) == EXPECTED_WORKFLOWS
assert len(domains) == 13
assert {item.mode for item in skills.values()} == set(RequestMode)
assert all(item.functional_focus and item.source_priority for item in domains.values())
assert all(item.primary_skill in skills for item in workflows.values())
assert all(set(item.component_skills) <= set(skills) for item in workflows.values())
assert validate_skill_bundle(packaged_skills_root()) == []
print("FUNCTIONAL REVIEW: PASS")
print(f"skills={len(skills)} workflows={len(workflows)} domains={len(domains)}")
