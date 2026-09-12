from __future__ import annotations

from .project_models import AgentRole, TeamCyclePlan, WorkActivationCondition, WorkExecutionMode
from .project_service import ProjectService


class TeamOrchestrator:
    """Goal-oriented work-graph orchestration; execution providers remain below this layer."""

    def __init__(self, service: ProjectService) -> None:
        self.service = service

    def bootstrap_developer_cycle(self, project_id: str) -> list[str]:
        snapshot = self.service.snapshot(project_id)
        if snapshot.work_items:
            raise ValueError("developer cycle already contains work items")
        ids: list[str] = []
        discover = self.service.add_work_item(
            project_id,
            title="Discover and research",
            objective="Collect only the project facts, constraints, unknowns, and external evidence needed to execute the target correctly.",
            role=AgentRole.RESEARCHER,
            expected_outputs=["bounded research/evidence record", "blocking unknowns"],
        )
        ids.append(discover.work_item_id)
        design = self.service.add_work_item(
            project_id,
            title="Design",
            objective="Produce the smallest complete design that satisfies the target and known constraints.",
            role=AgentRole.DESIGNER,
            dependencies=[discover.work_item_id],
            expected_outputs=["design decisions", "interfaces/contracts", "material trade-offs"],
        )
        ids.append(design.work_item_id)
        plan = self.service.add_work_item(
            project_id,
            title="Implementation plan",
            objective="Turn the accepted design into an executable work plan with dependency order and verification points.",
            role=AgentRole.PLANNER,
            dependencies=[design.work_item_id],
            expected_outputs=["implementation work plan"],
        )
        ids.append(plan.work_item_id)
        implement = self.service.add_work_item(
            project_id,
            title="Implement",
            objective="Modify the project to implement the planned change while preserving established contracts.",
            role=AgentRole.IMPLEMENTER,
            execution_mode=WorkExecutionMode.EXTERNAL_AGENT,
            dependencies=[plan.work_item_id],
            expected_outputs=["source changes", "tests", "implementation record"],
        )
        ids.append(implement.work_item_id)
        verify = self.service.add_work_item(
            project_id,
            title="Build and verify",
            objective="Build/test the implementation and collect runtime or test evidence for the target acceptance criteria.",
            role=AgentRole.VERIFIER,
            dependencies=[implement.work_item_id],
            expected_outputs=["verification evidence", "remaining failures"],
        )
        ids.append(verify.work_item_id)
        review = self.service.add_work_item(
            project_id,
            title="Independent review",
            objective="Independently review the implementation and evidence, reporting only material findings that affect correctness, risk, confidence, or release readiness.",
            role=AgentRole.REVIEWER,
            dependencies=[verify.work_item_id],
            expected_outputs=["material findings", "required fixes"],
        )
        ids.append(review.work_item_id)
        fix = self.service.add_work_item(
            project_id,
            title="Resolve findings",
            objective="Resolve material review findings without widening scope beyond what the target requires.",
            role=AgentRole.IMPLEMENTER,
            execution_mode=WorkExecutionMode.EXTERNAL_AGENT,
            dependencies=[review.work_item_id],
            expected_outputs=["fixes", "updated tests/evidence"],
            activation_condition=WorkActivationCondition.OPEN_FINDINGS,
        )
        ids.append(fix.work_item_id)
        release_verify = self.service.add_work_item(
            project_id,
            title="Acceptance verification",
            objective="Verify every target acceptance criterion from concrete evidence and identify any criterion that remains unsatisfied.",
            role=AgentRole.VERIFIER,
            dependencies=[fix.work_item_id],
            expected_outputs=["acceptance evidence mapping", "release decision"],
        )
        ids.append(release_verify.work_item_id)
        return ids

    def continue_project(self, project_id: str) -> TeamCyclePlan:
        return self.service.cycle_plan(project_id)
