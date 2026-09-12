# Online Pre-Request Planner

The planner is an online reconnaissance pass. It may search current sources but must not solve the engineering problem.

Input:

- sanitized request metadata (no artifact bodies; no candidate metadata in design mode),
- deterministic RoutingDecision,
- skill contracts,
- domain source/focus policies,
- a bounded capability allowlist.

Output: `ResearchManifest` containing research questions, local context requirements, online research tasks, source priorities, evidence requirements, tool plan, missing evidence, budget and report contract.

The validator rejects capabilities not present in RoutingDecision and rejects report contracts missing required sections. Verify mode requires explicit evidence requirements.
