# MCP tool contract

The MCP server is a thin typed gateway to `ReviewEngine`.

Tools:

1. `prepare_request` — manual/interactive planner start.
2. `run_request_automatic` — automatic planner + final browser workflow.
3. `submit_planner_result` — import planner ResearchManifest.
4. `submit_final_result` — import FinalReport.
5. `get_request_status` — workflow state.
6. `inspect_routing` — deterministic skill/workflow/domain/capability decision without a model call.
7. `inspect_context` — filtered ContextPack after planner stage.
8. `list_skills` — six primitive skills.
9. `list_workflows` — four orchestration profiles.
10. `list_domains` — thirteen technical policies.

Repository access is constrained by `FANCY_GPT_ALLOWED_ROOTS`.
