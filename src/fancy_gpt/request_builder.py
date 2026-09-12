from __future__ import annotations

from .models import ContextPack, FinalReport, ModelRequest, RawRequest, ResearchManifest, RoutingDecision
from .planner import PreRequestPlanner
from .prompt_compiler import FinalPromptCompiler


class PlannerRequestBuilder:
    def __init__(self, planner: PreRequestPlanner | None = None) -> None:
        self.planner = planner or PreRequestPlanner()

    def build(self, request_id: str, request: RawRequest, route: RoutingDecision) -> ModelRequest:
        return ModelRequest(
            request_id=request_id,
            stage="planner",
            title=f"Pre-request plan: {route.route_name}",
            prompt=self.planner.build_prompt(request_id, request, route),
            response_schema=ResearchManifest.model_json_schema(),
            metadata={
                "route_kind": route.route_kind,
                "route_name": route.route_name,
                "skill": route.primary_skill,
                "mode": request.mode.value,
            },
        )


class FinalRequestBuilder:
    def __init__(self, compiler: FinalPromptCompiler | None = None) -> None:
        self.compiler = compiler or FinalPromptCompiler()

    def build(
        self,
        request_id: str,
        request: RawRequest,
        route: RoutingDecision,
        manifest: ResearchManifest,
        context: ContextPack,
    ) -> ModelRequest:
        return ModelRequest(
            request_id=request_id,
            stage="final",
            title=f"Final run: {route.route_name}",
            prompt=self.compiler.build_prompt(request_id, request, route, manifest, context),
            response_schema=FinalReport.model_json_schema(),
            metadata={
                "route_kind": route.route_kind,
                "route_name": route.route_name,
                "skill": route.primary_skill,
                "mode": request.mode.value,
                "context_hash": context.context_hash,
            },
        )
