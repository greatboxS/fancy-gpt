from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from .context_builder import ContextBuilder
from .conversations import ConversationManager
from .errors import InvalidStateError
from .inspection import SessionInspectionService
from .models import (
    ContextPack,
    ChatRecord,
    ChatPolicy,
    ChatResolution,
    FinalReport,
    InteractionRequired,
    ModelRequest,
    RawRequest,
    RequestState,
    RequestStatus,
    ResearchManifest,
    RoutingDecision,
    SessionRecord,
    SessionInspection,
)
from .planner import PreRequestPlanner
from .providers import ChatGPTWebInteractiveProvider
from .providers.base import AutomaticModelProvider
from .request_builder import FinalRequestBuilder, PlannerRequestBuilder
from .response_parser import parse_json_object
from .stage_json import parse_or_reask
from .routing import RequestClassifier
from .store import RequestStore, SessionStore


class ReviewEngine:
    def __init__(
        self,
        workdir: Path | str = ".fancy-gpt",
        *,
        allowed_roots: list[Path | str] | None = None,
    ) -> None:
        self.store = RequestStore(Path(workdir))
        self.session_store = SessionStore(Path(workdir))
        self.conversations = ConversationManager(self.session_store)
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

    def _new_request(
        self, request: RawRequest, route: RoutingDecision, resolution: ChatResolution, request_id: str | None = None
    ) -> str:
        request_id = request_id or uuid.uuid4().hex[:16]
        self.store.create_status(
            request_id,
            route_kind=route.route_kind,
            route_name=route.route_name,
            skill=route.primary_skill,
            mode=request.mode,
            objective=request.objective,
            session_id=resolution.session_id,
            chat_id=resolution.chat_id,
            chat_policy=resolution.policy,
        )
        self.store.write_model(request_id, "request.json", request)
        self.store.write_model(request_id, "routing-decision.json", route)
        self.conversations.attach_request(resolution, request_id)
        return request_id

    def _resolution_for_status(self, status: RequestStatus) -> ChatResolution:
        conversation_id = status.conversation_id
        if status.session_id and status.chat_id:
            conversation_id = self.session_store.get_chat(status.session_id, status.chat_id).conversation_id
        return ChatResolution(
            policy=status.chat_policy or ChatPolicy.TEMPORARY,
            session_id=status.session_id,
            chat_id=status.chat_id,
            conversation_id=conversation_id,
        )

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
        resolution = self.conversations.resolve(request)
        request_id = self._new_request(request, route, resolution)
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
            resolution = self._resolution_for_status(status)
            model_request = self.final_request_builder.build(request_id, request, route, manifest, context, resolution)
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
        resolution = self.conversations.resolve(request)
        request_id = self._new_request(request, route, resolution, request_id=request_id)
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
            planner_response = provider.execute(
                planner_request, on_progress=lambda text: self.store.update_progress(request_id, text)
            )
            planner_response_path = self.store.write_model(request_id, "planner-response.json", planner_response)
            emit("manifest-validate")
            planner_payload, planner_response = parse_or_reask(
                provider,
                planner_request,
                planner_response,
                parse_json_object,
                on_progress=lambda text: self.store.update_progress(request_id, text),
                on_response=lambda reply, attempt: self.store.write_model(
                    request_id,
                    "planner-response.json" if attempt == 0 else f"planner-response-reask-{attempt}.json",
                    reply,
                ),
            )
            manifest = self._validate_manifest(planner_payload, route)
            self.store.write_model(request_id, "research-manifest.json", manifest)

            emit("context-build")
            context = self.context_builder.build(request, manifest)
            self.store.write_model(request_id, "context-pack.json", context)
            emit("final-dispatch")
            final_request = self.final_request_builder.build(
                request_id, request, route, manifest, context, resolution
            )
            final_prompt_path = self.store.write_text(request_id, "final-prompt.md", final_request.prompt)
            self.store.transition(
                request_id,
                RequestState.RUNNING_PLANNER,
                RequestState.RUNNING_FINAL,
                planner_response_file=str(planner_response_path),
                final_prompt_file=str(final_prompt_path),
            )

            chat_lock = (
                self.session_store.chat_operation_lock(resolution.session_id, resolution.chat_id)
                if resolution.session_id and resolution.chat_id
                else nullcontext()
            )
            with chat_lock:
                # Re-read the conversation after waiting for another request
                # on this chat; that request may have created its provider id.
                if resolution.session_id and resolution.chat_id:
                    chat = self.session_store.get_chat(resolution.session_id, resolution.chat_id)
                    if chat.conversation_id:
                        final_request.metadata["conversation_id"] = chat.conversation_id
                emit("final-wait")
                final_response = provider.execute(
                    final_request, on_progress=lambda text: self.store.update_progress(request_id, text)
                )
                # Persist provider identity before parsing/validation so a valid
                # ChatGPT conversation remains recoverable after downstream errors.
                self.conversations.bind_conversation(
                    resolution,
                    final_response.conversation_id,
                    getattr(provider, "tunnel_id", None),
                )
                self.store.update_status(request_id, conversation_id=final_response.conversation_id)
                final_response_path = self.store.write_model(request_id, "final-response.json", final_response)
                emit("report-validate")
                final_payload, final_response = parse_or_reask(
                    provider,
                    final_request,
                    final_response,
                    parse_json_object,
                    on_response=lambda reply, attempt: self.store.write_model(
                        request_id,
                        "final-response.json" if attempt == 0 else f"final-response-reask-{attempt}.json",
                        reply,
                    ),
                )
                report = self._validate_report(
                    request_id,
                    request,
                    route,
                    manifest,
                    final_payload,
                )
                result_path = self.store.write_model(request_id, "final-result.json", report)
                self.store.transition(
                    request_id,
                    RequestState.RUNNING_FINAL,
                    RequestState.COMPLETE,
                    final_response_file=str(final_response_path),
                    result_file=str(result_path),
                    conversation_id=final_response.conversation_id,
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

    def create_session(self, repo_root: str, title: str | None = None) -> SessionRecord:
        self.context_builder.validate_allowed_root(repo_root, self.allowed_roots)
        return self.conversations.create_session(repo_root, title)

    def get_session(self, session_id: str) -> SessionRecord:
        return self.session_store.get_session(session_id)

    def list_sessions(self) -> list[SessionRecord]:
        return self.session_store.list_sessions()

    def close_session(self, session_id: str) -> SessionRecord:
        return self.session_store.close_session(session_id)

    def create_chat(
        self, session_id: str, title: str, *, independent: bool = False, make_active: bool = True
    ) -> ChatRecord:
        return self.conversations.create_chat(
            session_id, title, independent=independent, make_active=make_active
        )

    def list_chats(self, session_id: str, *, include_archived: bool = False) -> list[ChatRecord]:
        return self.session_store.list_chats(session_id, include_archived=include_archived)

    def select_chat(self, session_id: str, chat_id: str) -> SessionRecord:
        return self.session_store.select_chat(session_id, chat_id)

    def archive_chat(self, session_id: str, chat_id: str) -> ChatRecord:
        return self.session_store.archive_chat(session_id, chat_id)

    def list_session_requests(self, session_id: str) -> list[RequestStatus]:
        session = self.session_store.get_session(session_id)
        request_ids = [
            request_id
            for chat in self.session_store.list_chats(session_id, include_archived=True)
            for request_id in chat.request_ids
        ]
        # Preserve requests even if future chat metadata contains a duplicate.
        return self.store.list_statuses(list(dict.fromkeys(request_ids)))

    def inspect_session(self, session_id: str) -> SessionInspection:
        return SessionInspectionService(self.session_store, self.store).inspect_session(session_id)

    def inspect_request(self, request_id: str):
        return SessionInspectionService.inspect_request(self.store.load_status(request_id))

    def list_requests(
        self, *, session_id: str | None = None, state: RequestState | None = None,
        kind: str | None = None, limit: int = 100,
    ):
        statuses = self.list_session_requests(session_id) if session_id else self.store.list_statuses()
        if state is not None:
            statuses = [item for item in statuses if item.state == state]
        if kind is not None:
            statuses = [item for item in statuses if item.kind == kind]
        return [SessionInspectionService.inspect_request(item) for item in statuses[:max(0, min(limit, 1000))]]

    def request_trace(self, request_id: str, *, summary_only: bool = False):
        """The recorded step-by-step history of one turn.

        Returns the events with a compact summary. The trace holds no prompt or
        reply content and no page URL by construction, so it is safe to read and
        to paste into a bug report.
        """
        from .gateway_trace import read_trace

        # Reuses the store's own id validation, so a crafted id cannot escape
        # the request directory.
        directory = self.store.request_dir(request_id)
        events = read_trace(directory / "trace.jsonl")
        stages: dict[str, int] = {}
        for event in events:
            stage = str(event.get("stage", ""))
            stages[stage] = max(stages.get(stage, 0), int(event.get("elapsed_ms", 0)))
        terminal = next(
            (item for item in reversed(events) if item.get("stage") == "terminal"), None
        )
        summary = {
            "request_id": request_id,
            "events": len(events),
            "total_ms": events[-1].get("elapsed_ms", 0) if events else 0,
            "stage_reached_ms": stages,
            "outcome": terminal.get("event") if terminal else None,
            "outcome_detail": terminal.get("detail", "") if terminal else "",
        }
        # Lineage is part of diagnosing a turn: it says how many attempts a
        # logical request took and which superseded which.
        lineage: list[dict] = []
        try:
            from .gateway_state import GatewayStateStore

            state = GatewayStateStore(self.store.root)
            record = state.load_turn(request_id)
            if record is not None:
                lineage = [
                    {
                        "response_id": item.response_id,
                        "attempt": item.attempt,
                        "state": item.state.value,
                        "retry_of": item.retry_of,
                        "created_at": item.created_at,
                    }
                    for item in state.retry_lineage(request_id)
                ]
                summary["state"] = record.state.value
                summary["attempt"] = record.attempt
                summary["transitions"] = [
                    {"state": item.state.value, "at": item.at, "detail": item.detail}
                    for item in record.transitions
                ]
        except Exception:
            # Inspection must never fail because a side record is unreadable.
            lineage = []
        summary["retry_lineage"] = lineage
        if summary_only:
            return {"summary": summary, "events": []}
        return {"summary": summary, "events": events}

    def request_raw_response(self, request_id: str) -> str | None:
        status = self.store.load_status(request_id)
        path_value = status.final_response_file or status.planner_response_file
        if not path_value:
            return None
        path = Path(path_value).expanduser().resolve()
        root = Path(self.store.root).expanduser().resolve()
        if not path.is_relative_to(root):
            raise ValueError("request response file is outside the work directory")
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def inspect_context(self, request_id: str) -> ContextPack:
        return self.store.read_model(request_id, "context-pack.json", ContextPack)

    def read_prompt(self, request_id: str, stage: str) -> str:
        if stage not in {"planner", "final"}:
            raise ValueError("stage must be planner or final")
        path = self.store.request_dir(request_id) / f"{stage}-prompt.md"
        return path.read_text(encoding="utf-8")
