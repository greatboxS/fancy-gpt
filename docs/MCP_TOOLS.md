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
11. `server_stats` — MCP process call/error counters plus a live bridge worker/job snapshot.

Repository access is constrained by `FANCY_GPT_ALLOWED_ROOTS`.

## Conversation continuity

`RawRequest` carries two fields that control which ChatGPT conversation a
`run_request_automatic`/`prepare_request` call lands in (extension-bridge
tunnels only; Playwright/CDP tunnels always start a fresh temporary chat):

- `conversation_mode`: `"persistent"` (default) opens a real, saved ChatGPT
  conversation instead of a `?temporary-chat=true` one, so it appears in the
  account's history and its resulting id can be reused. `"temporary"` keeps
  the previous ephemeral, non-resumable behavior.
- `conversation_id`: when set, the call continues that existing conversation
  (both the planner and final turns run inside it) instead of starting a new
  one. Ignored if empty.

After a call completes, call `get_request_status(request_id)` and read
`.conversation_id` from the response — pass that value as the next request's
`conversation_id` to keep the thread going across multiple
`run_request_automatic` calls in the same session. Omit both fields (or set
`conversation_mode: "temporary"`) for a one-off, unlinked request.

`get_request_status` also exposes `.partial_text` /
`.partial_text_updated_at`: a best-effort, ~1.5s-refreshed snapshot of what
ChatGPT is currently rendering while a planner/final turn is still in
flight (extension-bridge tunnels only). This is the visible answer text
only, not ChatGPT's hidden reasoning trace.
