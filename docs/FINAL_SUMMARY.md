# v0.8.0 summary

FancyGPT v0.8.0 changes the product boundary from an independent review utility into a **persistent engineering-team runtime**. Review remains a first-class capability, while projects can now retain targets, acceptance criteria, work graphs, sessions, decisions, evidence, findings, artifacts, and relevant conversation bindings across multiple engineering cycles.

The runtime adds semantic Relevance/Sufficiency policy so a small question is not automatically expanded into a broad report. Expansion is justified only when it materially changes correctness, decision quality, risk, confidence, a blocking unknown, or the next required action. This policy is propagated through planner, final review, focused-answer, and team-agent execution rather than implemented as a hard word limit.

Project state is append-only and models receive reduced relevant context instead of replayed raw chat history. The developer-cycle orchestrator supports researcher/designer/planner/model teammates and explicit external implementer handoffs for Codex/Claude. Project completion is evidence-backed: acceptance criteria, required work, and material findings must all be resolved.

ChatGPT Web is treated as a stateful reasoning workspace. Sessions may be `fresh`, `resume`, or `fork`; persistent threads store ChatGPT conversation URLs while independent reviewer/verifier roles default to fresh conversations to preserve independence.

The v0.7 Tunnel architecture remains the execution fabric beneath this runtime: Site + Runtime + Transport + Browser + deployment compose into runtime-selectable tunnels. CLI and MCP remain thin surfaces over shared services.
