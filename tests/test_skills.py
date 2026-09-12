from pathlib import Path

from fancy_gpt.catalog import load_skills
from fancy_gpt.skills import packaged_skills_root, validate_skill_bundle


def test_packaged_agent_skill_bundle_is_exactly_six_and_valid():
    errors=validate_skill_bundle(packaged_skills_root())
    assert errors==[]
    assert len(load_skills())==6
    assert len(list(packaged_skills_root().glob("*/SKILL.md")))==6


def test_top_level_skill_bundle_is_valid():
    root=Path(__file__).resolve().parents[1]/"skills"
    assert validate_skill_bundle(root)==[]
