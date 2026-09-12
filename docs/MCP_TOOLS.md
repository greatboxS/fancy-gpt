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
12. `create_session`, `get_session`, `list_sessions`, `close_session` — session lifecycle.
13. `create_chat`, `list_chats`, `select_chat`, `archive_chat` — multi-chat management.
14. `list_session_requests` — request history for all chats in a session.
15. `session_capabilities` — machine-readable behavior/profile advertisement.

Repository access is constrained by `FANCY_GPT_ALLOWED_ROOTS`.

## Conversation continuity

Planner turns are always temporary. Final turns are controlled by:

- `session_id`: explicit work session; omit it to use the repo-scoped default.
- `chat_id`: explicitly target a chat belonging to that session.
- `chat_policy`: `continue` (default), `new_chat`, `independent`, or `temporary`.

`independent` creates a durable review chat without changing the active chat.
`new_chat` creates and selects a durable chat. `temporary` creates no session
or chat. See `SESSION_ARCHITECTURE.md` for lifecycle and recovery rules.

The legacy fields remain supported:

- `conversation_mode`: `"persistent"` (default) opens a real, saved ChatGPT
  conversation instead of a `?temporary-chat=true` one, so it appears in the
  account's history and its resulting id can be reused. `"temporary"` keeps
  the previous ephemeral, non-resumable behavior.
- `conversation_id`: binds an existing provider conversation to the resolved
  final chat. The planner never uses it.

`get_request_status` exposes `.session_id`, `.chat_id`, `.chat_policy`, and
`.conversation_id`. Subsequent requests with the same session automatically
reuse its active chat; callers no longer need to copy provider ids manually.

`get_request_status` also exposes `.partial_text` /
`.partial_text_updated_at`: a best-effort, ~1.5s-refreshed snapshot of what
ChatGPT is currently rendering while a planner/final turn is still in
flight (extension-bridge tunnels only). This is the visible answer text
only, not ChatGPT's hidden reasoning trace.
