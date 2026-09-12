# Requirements checklist — v0.8.0

| Requirement | Status | Evidence |
|---|---|---|
| Tool/Engine and MCP are separate layers | PASS | `engine.py`, thin `mcp_server.py` |
| Python implementation | PASS | Python >=3.12 |
| Tool owns context → request → response lifecycle | PASS | routing/planner/context/request/provider/validators |
| Online pre-request planner | PASS (offline contract) | two-pass engine + provider boundary |
| Planner plans research/tools/evidence/report instead of final answer | PASS | ResearchManifest contract |
| Final model receives ResearchManifest + ContextPack | PASS | final request builder |
| Context supports files/glob/search/git diff | PASS | `ContextBuilder` |
| Independent-design isolation | PASS | candidate filtering + regression tests |
| Filesystem allowed-root confinement | PASS | engine preflight |
| ChatGPT Web Plus/no API key architecture | IMPLEMENTED / LIVE-VERIFY LOCAL | extension/Playwright/CDP paths |
| No Codex dependency | PASS | no Codex runtime/API path |
| No OpenAI API key dependency | PASS | web provider only |
| 6 compact reasoning skills | PASS | Agent Skill bundles |
| 4 workflows separated from skills | PASS | workflow catalog |
| 13 technical domains | PASS | domain catalog |
| Install once, global CLI | PASS | `install.sh` + bundled wheel |
| Runtime-selectable Tunnel abstraction | PASS | tunnel spec/registry/resolver/manager |
| Site layer independently testable | PASS | `web/sites` + tests |
| Runtime layer independently testable | PASS | `web/runtime` + tests |
| Transport layer independently testable | PASS | `web/transport` + tests |
| Tunnel composition validates cross-layer compatibility | PASS | `TunnelCompositionValidator` |
| Chrome extension support | PASS (offline package) | Chromium manifest/assets |
| Edge extension support | PASS (offline package) | Chromium-compatible export with Edge defaults |
| Firefox extension support | PASS (offline package) | Firefox manifest/background/native semantics |
| Native Messaging local tunnel | PASS (offline contract) | native host + manifests |
| Remote browser tunnel through WebSocket + SSH | PASS (offline bridge contract) | bridge server/client + extension transport |
| Playwright tunnel | PASS (offline contract) | page runtime + site adapter composition |
| CDP/native-browser attach tunnel | PASS (offline contract) | CDP runtime + site adapter |
| Interactive fallback | PASS | tunnel catalog |
| Select tunnel via request/CLI | PASS | `tunnel`, `tunnel_policy`, `--tunnel` |
| Select/inspect tunnel via MCP | PASS | list/probe/inspect/select/component tools |
| Multiple tunnel endpoints/tokens | PASS | per-tunnel environment overrides/config |
| Custom tunnel catalog | PASS | explicit `FANCY_GPT_TUNNELS_FILE` |
| Bridge default loopback-only | PASS | non-loopback requires explicit opt-in |
| Browser credentials not copied/extracted | PASS | browser/session boundary |
| Layered extension implementation | PASS | transport/runtime/site/dispatcher JS |
| Deterministic extension build/package | PASS | `scripts/build_extension.py --check` |
| Offline package self-test | PASS | `fancy-gpt test` |
| Real ChatGPT UI/session compatibility | LOCAL VERIFICATION REQUIRED | external site UI |
| Real MCP SDK import in offline build container | ENVIRONMENT-DEPENDENT | dependency declared; mocked contract test PASS |

## v0.8 persistent-team requirements

| Requirement | Status | Evidence |
|---|---|---|
| Persistent Project/Target state | PASS | `project_models.py`, `project_store.py` |
| Append-only project history | PASS | `ProjectStore` event journal |
| Relevant state reduction instead of raw chat replay | PASS | `ProjectService.relevant_context` |
| Semantic scope/relevance policy, not word-count control | PASS | `relevance.py`, planner/final/focused/team prompts |
| Focused one-pass answer path for small questions | PASS | `fancy-gpt ask`, MCP `ask_focused` |
| Team roles separated from model/tunnel provider | PASS | `AgentRole`, `TeamAgentEngine`, `TunnelManager` |
| Developer-cycle orchestration | PASS | `TeamOrchestrator`, `ProjectRunner` |
| External Codex/Claude implementation handoff | PASS | `WorkExecutionMode.EXTERNAL_AGENT`, `AgentAssignment`, `submit_agent_outcome` |
| Evidence-backed acceptance completion | PASS | acceptance normalization + verifier criterion mapping |
| New verifier evidence usable in same outcome | PASS | `EvidenceDraft.ref` / `CriterionAssessmentDraft.evidence_refs` |
| Structured finding resolution from implementer | PASS | `finding_resolutions` in `AgentOutcome` |
| Clean review skips unnecessary fix stage | PASS | `WorkActivationCondition.OPEN_FINDINGS` / `SKIPPED` |
| Stateful ChatGPT Web session strategy | PASS (offline contract) | fresh/resume/fork + persisted URL binding |
| Execution diagnostics cover review/focused/team | PASS | `ExecutionCoordinator` |
| Outbound secret/DLP policy | PASS | `SecretPolicy` + ContextBuilder integration |
| Bounded large-repo acquisition | PASS | file/byte/time budgets + bounded Git |
