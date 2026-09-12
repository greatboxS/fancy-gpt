---
name: technical-review
description: Challenge an existing artifact, implementation, configuration, or design against evidence.
compatibility: fancy-gpt MCP/CLI
---

# technical-review

Use this skill when the requested reasoning mode is **review**. Technical specialization belongs in the request `domains` and `focus`; do not create a new skill for a narrow technology.

## Required inputs

- Objective and constraints.
- Relevant domain(s) and focused technical questions.
- Local artifacts only when they are needed; mark a design candidate as `role: candidate-solution`.
- Desired freshness/risk level when version-sensitive or production-critical.

## Workflow

1. Route the request through fancy-gpt using this canonical skill.
2. Let the online pre-request planner identify research questions, source priorities, local context requirements, and evidence requirements.
3. Let the ContextBuilder collect only the filtered local evidence.
4. Run the final online model with the ResearchManifest + ContextPack.
5. Reject outputs that do not satisfy the structured mode contract and evidence gates.

## Output contract

Required report sections: executive-summary, findings, recommendation, unknowns, validation-plan.

Quality gates:
- Findings explain mechanism and impact, not only symptoms.
- Recommendations stay within stated constraints.

Do not bypass the planner/context/evidence pipeline by directly asking the model to solve the task.
