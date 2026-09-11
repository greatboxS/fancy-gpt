from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

import yaml

from .catalog import load_skills


def packaged_skills_root() -> Path:
    return Path(str(files("fancy_gpt").joinpath("agent_skills")))


def validate_skill_bundle(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    errors: list[str] = []
    catalog = load_skills()
    found: set[str] = set()
    if not root.exists():
        return [f"skill root does not exist: {root}"]
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        skill_file = directory / "SKILL.md"
        if not skill_file.exists():
            errors.append(f"{directory.name}: missing SKILL.md")
            continue
        text = skill_file.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            errors.append(f"{directory.name}: missing YAML frontmatter")
            continue
        try:
            _, raw_frontmatter, _ = text.split("---", 2)
            frontmatter = yaml.safe_load(raw_frontmatter) or {}
        except Exception as exc:
            errors.append(f"{directory.name}: invalid frontmatter: {exc}")
            continue
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not isinstance(name, str) or not name:
            errors.append(f"{directory.name}: frontmatter name is required")
            continue
        if not isinstance(description, str) or not description:
            errors.append(f"{directory.name}: frontmatter description is required")
            continue
        if name != directory.name:
            errors.append(f"{directory.name}: frontmatter name must match directory")
        if name not in catalog:
            errors.append(f"{directory.name}: not present in canonical skill catalog")
        found.add(name)
    missing = set(catalog) - found
    extra = found - set(catalog)
    if missing:
        errors.append(f"missing skill bundles: {sorted(missing)}")
    if extra:
        errors.append(f"unknown skill bundles: {sorted(extra)}")
    return errors


def export_packaged_skills(destination: Path) -> list[Path]:
    source = packaged_skills_root()
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for skill in load_skills():
        src = source / skill / "SKILL.md"
        dst_dir = destination / skill
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "SKILL.md"
        shutil.copy2(src, dst)
        exported.append(dst)
    return exported
