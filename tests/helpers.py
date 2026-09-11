from __future__ import annotations

from fancy_gpt.models import RequestMode


def planner_payload(mode: str = "review", domains: list[str] | None = None, required_sections: list[str] | None = None) -> dict:
    domains = domains or ["architecture"]
    sections = required_sections or ["executive-summary", "findings", "recommendation", "unknowns", "validation-plan"]
    evidence = []
    if mode == "verify":
        evidence = [{"id":"EV1","claim_area":"claim","required_evidence":["runtime evidence"],"acceptance_rule":"must be measured"}]
    return {
        "intent": [mode],
        "domains": domains,
        "tags": ["test"],
        "research_goal": "collect evidence without solving in planner",
        "research_questions": [{"id":"RQ1","question":"What evidence decides this?","rationale":"Discriminate claims","priority":"P0"}],
        "local_context_requirements": [{"id":"LC1","description":"design docs","patterns":["docs/*.md"],"exact_paths":[],"search_terms":[],"priority":"P0","required":True}],
        "online_research": [{"id":"WEB1","objective":"Verify authoritative behavior","queries":["authoritative behavior"],"source_priority":["official docs"],"freshness":"version-specific","expected_evidence":["documented behavior"]}],
        "source_map": [],
        "evidence_requirements": evidence,
        "tool_plan": [{"capability":"repo.read","purpose":"read local context","required":True},{"capability":"web.search","purpose":"verify current behavior","required":True}],
        "freshness":"version-specific",
        "missing_evidence":[],
        "report_contract":{"sections":sections,"require_citations":True,"require_evidence_mapping":True,"require_unknowns":True,"require_validation_plan":True,"style":"technical"},
        "research_budget":{"max_search_queries":5,"max_open_pages":5,"max_context_bytes":100000},
    }


def final_payload(request_id: str, mode: str = "review", sections: list[str] | None = None, evidence_requirement: bool = False) -> dict:
    sections = sections or ["executive-summary", "findings", "recommendation", "unknowns", "validation-plan"]
    payload = {
        "request_id": request_id,
        "mode": mode,
        "status":"complete",
        "verdict":"needs-changes",
        "executive_summary":"Evidence-backed result.",
        "report_sections_completed": sections,
        "findings":[{"id":"F1","severity":"high","category":"architecture","claim":"Ownership is ambiguous.","evidence":[{"source":"docs/design.md","locator":"A -> B","claim_supported":"Ownership is not defined."}],"impact":"Recovery can diverge.","recommendation":"Define one owner."}],
        "options":[],
        "hypotheses":[],
        "deliverables":[],
        "recommendation":"Clarify ownership.",
        "assumptions_challenged":[],
        "unknowns":[],
        "validation_plan":[{"step":"Trace lifecycle","expected_evidence":"owner map","pass_condition":"one owner"}],
        "research_trace":[{"task_id":"WEB1","status":"completed","source_urls":["https://example.org/official"],"note":"primary evidence"}],
        "evidence_coverage":[],
        "sources":[{"title":"Official source","url":"https://example.org/official","authority":"primary","used_for":["WEB1"]}],
        "confidence":0.8,
    }
    if mode == "consult":
        payload["options"]=[
            {"name":"A","description":"Option A","advantages":["simple"],"disadvantages":["limited"],"risks":[],"when_to_choose":"when simplicity dominates"},
            {"name":"B","description":"Option B","advantages":["scalable"],"disadvantages":["complex"],"risks":["cost"],"when_to_choose":"when scale dominates"},
        ]
    if mode == "design":
        payload["deliverables"]=[{"name":"design.md","kind":"design","content":"# Complete Design\n\nComponents and interfaces are defined.\n"}]
    if mode == "investigate":
        payload["hypotheses"]=[{"id":"H1","hypothesis":"Race condition","mechanism":"unsynchronized ownership","evidence_for":[],"evidence_against":[],"confidence":0.5,"next_discriminating_test":"instrument ownership"}]
    if mode == "write":
        payload["deliverables"]=[{"name":"artifact.md","kind":"document","content":"# Finished artifact\n\nThis is reusable written content.\n"}]
    if mode == "verify" or evidence_requirement:
        if mode == "verify":
            payload["verdict"]="insufficient"
        payload["evidence_coverage"]=[{"requirement_id":"EV1","status":"partial","evidence":[{"source":"runtime","locator":"counter","claim_supported":"some proof"}],"note":"more needed"}]
    return payload
