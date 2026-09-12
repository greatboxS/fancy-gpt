from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Callable

from .context_builder import ContextBuilder
from .errors import InvalidStateError
from .models import (
    ContextPack,
    FinalReport,
    InteractionRequired,
    RawRequest,
    RequestState,
    RequestStatus,
    ResearchManifest,
    RoutingDecision,
)
from .planner import PreRequestPlanner
from .providers import ChatGPTWebInteractiveProvider
from .providers.base import AutomaticModelProvider
from .request_builder import FinalRequestBuilder, PlannerRequestBuilder
from .response_parser import parse_json_object
from .routing import RequestClassifier
from .store import RequestStore


class ReviewEngine:
    def __init__(
        self,
        workdir: Path | str = ".fancy-gpt",
        *,
        allowed_roots: list[Path | str] | None = None,
    ) -> None:
        self.store = RequestStore(Path(workdir))
        self.context_builder = ContextBuilder()
        self.planner = PreRequestPlanner()
        self.router = RequestClassifier()
        self.planner_request_builder = PlannerRequestBuilder(self.planner)
        self.final_request_builder = FinalRequestBuilder()
        self.interactive_provider = ChatGPTWebInteractiveProvider()
        self.allowed_roots = self._resolve_allowed_roots(allowed_roots)

    @staticmethod
    def _resolve_allowed_roots(values: list[Path | str] | None) -> list[Path]:
        if values is not None:
            roots = [Path(value).expanduser().resolve() for value in values]
        else:
            env = os.getenv("FANCY_GPT_ALLOWED_ROOTS", "").strip()
            if env:
                roots = [Path(item).expanduser().resolve() for item in env.split(os.pathsep) if item]
            else:
                roots = [Path.cwd().resolve()]
        if not roots:
            raise ValueError("at least one allowed root is required")
        return roots

    def route(
        self,
        request: RawRequest,
        *,
        skill_name: str | None = None,
        workflow_name: str | None = None,
    ) -> RoutingDecision:
        self.context_builder.validate_allowed_root(request.repo_root, self.allowed_roots)
        return self.router.classify(request, skill_name=skill_name, workflow_name=workflow_name)

    def _new_request(self, request: RawRequest, route: RoutingDecision, request_id: str | None = None) -> str:
        request_id = request_id or uuid.uuid4().hex[:16]
        self.store.create_status(
            request_id,
            route_kind=route.route_kind,
            route_name=route.route_name,
            skill=route.primary_skill,
            mode=request.mode,
            objective=request.objective,
        )
        self.store.write_model(request_id, "request.json", request)
        self.store.write_model(request_id, "routing-decision.json", route)
        return request_id

    def _validate_manifest(self, payload: dict, route: RoutingDecision) -> ResearchManifest:
        manifest = ResearchManifest.model_validate(payload)
        return self.planner.validate_manifest(manifest, route)

    @staticmethod
    def _validate_report(
        request_id: str,
        request: RawRequest,
        route: RoutingDecision,
        manifest: ResearchManifest,
        payload: dict,
    ) -> FinalReport:
        report = FinalReport.model_validate(payload)
        if report.mode != request.mode:
            raise ValueError(f"result mode {report.mode.value} does not match request mode {request.mode.value}")
        if report.request_id != request_id:
            raise ValueError(f"result request_id {report.request_id} does not match {request_id}")

        missing_sections = set(route.required_sections) - set(report.report_sections_completed)
        if missing_sections:
            raise ValueError(f"final report omitted required sections: {sorted(missing_sections)}")

        planned_tasks = {task.id for task in manifest.online_research}
        traced_tasks = {item.task_id for item in report.research_trace}
        missing_tasks = planned_tasks - traced_tasks
        if missing_tasks:
            raise ValueError(f"final report did not account for online research tasks: {sorted(missing_tasks)}")

        evidence_reqs = {item.id for item in manifest.evidence_requirements}
        covered_reqs = {item.requirement_id for item in report.evidence_coverage}
        missing_evidence = evidence_reqs - covered_reqs
        if missing_evidence:
            raise ValueError(f"final report did not account for evidence requirements: {sorted(missing_evidence)}")

        declared_urls = {source.url for source in report.sources if source.url}
        traced_urls = {url for trace in report.research_trace for url in trace.source_urls}
        if not traced_urls.issubset(declared_urls):
            missing_urls = sorted(traced_urls - declared_urls)
            raise ValueError(f"research trace references URLs absent from sources: {missing_urls}")

        if manifest.report_contract.require_validation_plan and not report.validation_plan:
            raise ValueError("final report requires a validation plan")
        if request.mode.value == "consult" and not report.recommendation:
            raise ValueError("consult mode requires an explicit recommendation")
        if request.mode.value == "design" and "alternatives" in route.required_sections and not report.options:
            raise ValueError("design route requiring alternatives must include option analysis")
        if route.route_name == "deep-design-review" and not report.options:
            raise ValueError("deep-design-review requires alternatives/options")

        allowed_triggers = set(request.relevance_policy.expansion_triggers)
        for expansion in report.relevance_assessment.necessary_expansions:
            if expansion.trigger not in allowed_triggers:
                raise ValueError(f"relevance expansion uses disallowed trigger: {expansion.trigger.value}")
        if not report.relevance_assessment.within_requested_scope and not report.relevance_assessment.necessary_expansions:
            raise ValueError("out-of-scope final report must justify material scope expansion")
        return report

    # ------------------------- interactive workflow -------------------------

    def prepare(
        self,
        request: RawRequest,
        *,
        skill_name: str | None = None,
        workflow_name: str | None = None,
        open_browser: bool = False,
    ) -> InteractionRequired:
        route = self.route(request, skill_name=skill_name, workflow_name=workflow_name)
        request_id = self._new_request(request, route)
        model_request = self.planner_request_builder.build(request_id, request, route)
        interaction = self.interactive_provider.prepare(
            model_request,
            self.store.request_dir(request_id) / "planner-prompt.md",
            open_browser=open_browser,
        )
        self.store.transition(
            request_id,
            RequestState.NEW,
            RequestState.WAITING_PLANNER,
            provider=self.interactive_provider.name,
            planner_prompt_file=interaction.prompt_file,
        )
        return interaction

    def submit_planner(self, request_id: str, payload: dict, *, open_browser: bool = False) -> InteractionRequired:
        with self.store.operation_lock(request_id):
            status = self.store.load_status(request_id)
            if status.state != RequestState.WAITING_PLANNER:
                raise InvalidStateError(f"planner result not accepted in state {status.state.value}")
            request = self.store.read_model(request_id, "request.json", RawRequest)
            self.context_builder.validate_allowed_root(request.repo_root, self.allowed_roots)
            route = self.store.read_model(request_id, "routing-decision.json", RoutingDecision)
            manifest = self._validate_manifest(payload, route)
            self.store.write_model(request_id, "research-manifest.json", manifest)
            context = self.context_builder.build(request, manifest)
            self.store.write_model(request_id, "context-pack.json", context)
            model_request = self.final_request_builder.build(request_id, request, route, manifest, context)
            interaction = self.interactive_provider.prepare(
                model_request,
                self.store.request_dir(request_id) / "final-prompt.md",
                open_browser=open_browser,
            )
            self.store.transition(
                request_id,
                RequestState.WAITING_PLANNER,
                RequestState.WAITING_FINAL,
                final_prompt_file=interaction.prompt_file,
            )
            return interaction

    def submit_final(self, request_id: str, payload: dict) -> FinalReport:
        with self.store.operation_lock(request_id):
            status = self.store.load_status(request_id)
            if status.state != RequestState.WAITING_FINAL:
                raise InvalidStateError(f"final result not accepted in state {status.state.value}")
            request = self.store.read_model(request_id, "request.json", RawRequest)
            route = self.store.read_model(request_id, "routing-decision.json", RoutingDecision)
            manifest = self.store.read_model(request_id, "research-manifest.json", ResearchManifest)
            report = self._validate_report(request_id, request, route, manifest, payload)
            result_path = self.store.write_model(request_id, "final-result.json", report)
            self.store.transition(
                request_id,
                RequestState.WAITING_FINAL,
                RequestState.COMPLETE,
                result_file=str(result_path),
            )
            return report

    # ------------------------- automatic workflow ---------------------------

    def run_automatic(
        self,
        request: RawRequest,
        provider: AutomaticModelProvider,
        *,
        skill_name: str | None = None,
        workflow_name: str | None = None,
        request_id: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> FinalReport:
        emit = progress or (lambda _phase: None)
        route = self.route(request, skill_name=skill_name, workflow_name=workflow_name)
        request_id = self._new_request(request, route, request_id=request_id)
        provider_started = False
        try:
            emit("planner-dispatch")
            planner_request = self.planner_request_builder.build(request_id, request, route)
            planner_prompt_path = self.store.write_text(request_id, "planner-prompt.md", planner_request.prompt)
            self.store.transition(
                request_id,
                RequestState.NEW,
                RequestState.RUNNING_PLANNER,
                provider=provider.name,
                tunnel_id=getattr(provider, "tunnel_id", None),
                planner_prompt_file=str(planner_prompt_path),
            )

            emit("provider-start")
            provider.start()
            provider_started = True
            emit("planner-wait")
            planner_response = provider.execute(planner_request)
            planner_response_path = self.store.write_model(request_id, "planner-response.json", planner_response)
            emit("manifest-validate")
            manifest = self._validate_manifest(parse_json_object(planner_response.raw_text), route)
            self.store.write_model(request_id, "research-manifest.json", manifest)

            emit("context-build")
            context = self.context_builder.build(request, manifest)
            self.store.write_model(request_id, "context-pack.json", context)
            emit("final-dispatch")
            final_request = self.final_request_builder.build(request_id, request, route, manifest, context)
            final_prompt_path = self.store.write_text(request_id, "final-prompt.md", final_request.prompt)
            self.store.transition(
                request_id,
                RequestState.RUNNING_PLANNER,
                RequestState.RUNNING_FINAL,
                planner_response_file=str(planner_response_path),
                final_prompt_file=str(final_prompt_path),
            )

            emit("final-wait")
            final_response = provider.execute(final_request)
            final_response_path = self.store.write_model(request_id, "final-response.json", final_response)
            emit("report-validate")
            report = self._validate_report(
                request_id,
                request,
                route,
                manifest,
                parse_json_object(final_response.raw_text),
            )
            result_path = self.store.write_model(request_id, "final-result.json", report)
            self.store.transition(
                request_id,
                RequestState.RUNNING_FINAL,
                RequestState.COMPLETE,
                final_response_file=str(final_response_path),
                result_file=str(result_path),
            )
            emit("complete")
            return report
        except Exception as exc:
            self.store.fail(request_id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if provider_started:
                provider.stop()

    def status(self, request_id: str) -> RequestStatus:
        return self.store.load_status(request_id)

    def inspect_context(self, request_id: str) -> ContextPack:
        return self.store.read_model(request_id, "context-pack.json", ContextPack)

    def read_prompt(self, request_id: str, stage: str) -> str:
        if stage not in {"planner", "final"}:
            raise ValueError("stage must be planner or final")
        path = self.store.request_dir(request_id) / f"{stage}-prompt.md"
        return path.read_text(encoding="utf-8")
