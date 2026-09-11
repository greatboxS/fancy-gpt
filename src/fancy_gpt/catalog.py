from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

from .errors import CatalogError
from .models import DomainDefinition, SkillDefinition, WorkflowDefinition


def _catalog_path(name: str) -> Path:
    return Path(str(files("fancy_gpt").joinpath("catalog", name)))


def _load(name: str) -> dict:
    return yaml.safe_load(_catalog_path(name).read_text(encoding="utf-8")) or {}


def load_domains() -> dict[str, DomainDefinition]:
    result: dict[str, DomainDefinition] = {}
    for item in _load("domains.yaml").get("domains", []):
        obj = DomainDefinition.model_validate(item)
        if obj.name in result:
            raise CatalogError(f"duplicate domain: {obj.name}")
        result[obj.name] = obj
    return result


def load_skills() -> dict[str, SkillDefinition]:
    result: dict[str, SkillDefinition] = {}
    for item in _load("skills.yaml").get("skills", []):
        obj = SkillDefinition.model_validate(item)
        if obj.name in result:
            raise CatalogError(f"duplicate skill: {obj.name}")
        result[obj.name] = obj
    return result


def load_workflows() -> dict[str, WorkflowDefinition]:
    result: dict[str, WorkflowDefinition] = {}
    for item in _load("workflows.yaml").get("workflows", []):
        obj = WorkflowDefinition.model_validate(item)
        if obj.name in result:
            raise CatalogError(f"duplicate workflow: {obj.name}")
        result[obj.name] = obj
    return result


def get_skill(name: str) -> SkillDefinition:
    try:
        return load_skills()[name]
    except KeyError as exc:
        raise CatalogError(f"unknown skill: {name}") from exc


def get_workflow(name: str) -> WorkflowDefinition:
    try:
        return load_workflows()[name]
    except KeyError as exc:
        raise CatalogError(f"unknown workflow: {name}") from exc
