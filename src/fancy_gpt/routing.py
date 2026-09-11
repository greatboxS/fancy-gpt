from __future__ import annotations

from .capabilities import resolve_capabilities
from .catalog import get_skill, get_workflow, load_domains, load_skills
from .models import RawRequest, RoutingDecision


class RequestClassifier:
    """Deterministic request classifier.

    The caller selects a primitive skill or workflow. The classifier validates
    mode/domain compatibility and derives effective domains/focus. It does not
    ask an LLM to invent routing.
    """

    def classify(self, request: RawRequest, *, skill_name: str | None = None, workflow_name: str | None = None) -> RoutingDecision:
        if bool(skill_name) == bool(workflow_name):
            raise ValueError("select exactly one of skill_name or workflow_name")
        domains_catalog = load_domains()
        skills_catalog = load_skills()

        if workflow_name:
            wf = get_workflow(workflow_name)
            if request.mode != wf.mode:
                raise ValueError(f"workflow {workflow_name} requires mode={wf.mode.value}, got {request.mode.value}")
            if wf.primary_skill not in skills_catalog:
                raise ValueError(f"workflow {workflow_name} references unknown primary skill {wf.primary_skill}")
            unknown_components = [name for name in wf.component_skills if name not in skills_catalog]
            if unknown_components:
                raise ValueError(f"workflow {workflow_name} references unknown skills: {unknown_components}")
            route_kind = "workflow"
            route_name = wf.name
            primary_skill = wf.primary_skill
            component_skills = list(wf.component_skills)
            domains = _dedupe(wf.default_domains + request.domains)
            focus = _dedupe(wf.default_focus + request.focus)
            required_sections = _dedupe(wf.required_sections + skills_catalog[primary_skill].required_sections)
            quality_gates = _dedupe(wf.quality_gates + skills_catalog[primary_skill].quality_gates)
            if wf.name == "change-impact-assessment" and not (request.include_git_diff or request.artifacts or request.include):
                raise ValueError("change-impact-assessment requires git diff, changed artifacts, or an explicit include scope")
        else:
            skill = get_skill(skill_name or "")
            if request.mode != skill.mode:
                raise ValueError(f"skill {skill.name} requires mode={skill.mode.value}, got {request.mode.value}")
            route_kind = "skill"
            route_name = skill.name
            primary_skill = skill.name
            component_skills = [skill.name]
            domains = _dedupe(request.domains)
            focus = _dedupe(request.focus)
            required_sections = list(skill.required_sections)
            quality_gates = list(skill.quality_gates)

        if not domains:
            domains = ["architecture"] if request.mode.value in {"review", "design", "consult"} else ["testing"]
        unknown_domains = [name for name in domains if name not in domains_catalog]
        if unknown_domains:
            raise ValueError(f"unknown domains: {unknown_domains}")

        domain_caps: list[str] = []
        source_priority: list[str] = []
        for name in domains:
            domain = domains_catalog[name]
            domain_caps.extend(domain.default_capabilities)
            source_priority.extend(domain.source_priority)

        allowed = resolve_capabilities(domain_caps, request.freshness, request.include_git_diff)
        return RoutingDecision(
            route_kind=route_kind,
            route_name=route_name,
            mode=request.mode,
            primary_skill=primary_skill,
            component_skills=component_skills,
            domains=domains,
            focus=focus,
            allowed_capabilities=allowed,
            source_priority=_dedupe(source_priority),
            required_sections=_dedupe(required_sections),
            quality_gates=_dedupe(quality_gates),
        )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result
