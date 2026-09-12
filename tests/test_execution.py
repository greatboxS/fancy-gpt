from __future__ import annotations

import json
from pathlib import Path

import pytest

from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.execution import ExecutionCoordinator, ExecutionFailed, ExecutionPhase
from fancy_gpt.models import RawRequest, RequestMode
from fancy_gpt.providers import ChatGPTWebAutomationProvider
from fancy_gpt.tunnels.models import TunnelHealth, TunnelHealthState, TunnelSelection


class _Manager:
    def __init__(self, provider=None, fail_select: bool = False):
        self._provider = provider
        self.fail_select = fail_select

    def select(self, **kwargs):
        if self.fail_select:
            raise RuntimeError("bridge unavailable")
        return TunnelSelection(
            tunnel_id="fake",
            reason="test",
            explicit=True,
            health=TunnelHealth(tunnel_id="fake", state=TunnelHealthState.HEALTHY, detail="ok"),
        )

    def provider(self, selection):
        return self._provider


def test_execution_records_failure_before_request_engine_runs(tmp_path: Path) -> None:
    coordinator = ExecutionCoordinator(tmp_path, manager=_Manager(fail_select=True))
    request = RawRequest(mode=RequestMode.REVIEW, objective="Review current code")
    with pytest.raises(ExecutionFailed) as exc_info:
        coordinator.run_review(request, skill_name="technical-review")
    status = exc_info.value.status
    assert status.phase == ExecutionPhase.FAILED
    assert status.error is not None
    assert status.error.layer == "tunnel"
    assert status.request_id
    assert coordinator.store.load(status.execution_id).error.code == "TUNNEL_SELECTION_FAILED"


def test_execution_tracks_full_review_phases(tmp_path: Path) -> None:
    planner = {
        "intent": ["review"], "domains": ["architecture"], "tags": [], "research_goal": "review",
        "research_questions": [{"id":"q1","question":"What matters?","rationale":"scope","priority":"P1"}],
        "local_context_requirements": [], "online_research": [], "source_map": [], "evidence_requirements": [],
        "tool_plan": [], "freshness": "version-specific", "missing_evidence": [],
        "report_contract": {"sections":["executive-summary","findings","recommendation","unknowns","validation-plan"],"require_citations":True,"require_evidence_mapping":True,"require_unknowns":True,"require_validation_plan":True,"style":"concise"},
        "research_budget": {"max_search_queries":10,"max_open_pages":10,"max_context_bytes":180000}
    }
    def final(turn):
        return json.dumps({
            "request_id": turn.request_id, "mode":"review", "status":"complete", "verdict":"ok",
            "executive_summary":"ok", "report_sections_completed":["executive-summary","findings","recommendation","unknowns","validation-plan"],
            "findings":[],"options":[],"hypotheses":[],"deliverables":[],"recommendation":"none","assumptions_challenged":[],"unknowns":[],
            "validation_plan":[{"step":"verify","expected_evidence":"result","pass_condition":"passes"}],"research_trace":[],"evidence_coverage":[],"sources":[],"confidence":0.9
        })
    provider = ChatGPTWebAutomationProvider(FakeBrowserDriver([json.dumps(planner), final]))
    coordinator = ExecutionCoordinator(tmp_path, manager=_Manager(provider=provider))
    request = RawRequest(mode=RequestMode.REVIEW, objective="Review current code")
    report = coordinator.run_review(request, skill_name="technical-review")
    statuses = coordinator.store.list_recent()
    assert report.verdict == "ok"
    assert statuses[0].phase == ExecutionPhase.COMPLETE
    assert statuses[0].tunnel_id == "fake"
    assert statuses[0].request_id == report.request_id
