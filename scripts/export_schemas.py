from pathlib import Path
import json

from fancy_gpt.models import (
    AutomatedModelResponse,
    ChatRecord,
    ContextPack,
    FinalReport,
    RawRequest,
    RequestStatus,
    ResearchManifest,
    RoutingDecision,
    SessionCapabilities,
    SessionRecord,
)

out = Path(__file__).resolve().parents[1] / "schemas"
out.mkdir(exist_ok=True)
for cls, name in [
    (RawRequest, "raw-request.schema.json"),
    (ResearchManifest, "research-manifest.schema.json"),
    (ContextPack, "context-pack.schema.json"),
    (FinalReport, "final-report.schema.json"),
    (AutomatedModelResponse, "automated-model-response.schema.json"),
    (RequestStatus, "request-status.schema.json"),
    (RoutingDecision, "routing-decision.schema.json"),
    (SessionRecord, "session-record.schema.json"),
    (ChatRecord, "chat-record.schema.json"),
    (SessionCapabilities, "session-capabilities.schema.json"),
]:
    (out / name).write_text(json.dumps(cls.model_json_schema(), indent=2), encoding="utf-8")
