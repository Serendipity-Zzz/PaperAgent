from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from paperagent.agents.evidence import EvidenceBundle
from paperagent.agents.outline import OutlinePlan
from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.orchestration.failure import FailureRecord, RecoveryPlanner
from paperagent.orchestration.fallback_plans import safe_fallback_plan
from paperagent.orchestration.plan_models import CandidatePlan
from paperagent.orchestration.plan_validation import PlanValidator
from paperagent.orchestration.planner import CandidatePlanGenerator


class AssignmentStatus(StrEnum):
    PLANNED = "planned"
    WAITING_APPROVAL = "waiting_approval"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HUMAN_REQUIRED = "human_required"


class ApprovalCard(BaseModel):
    approval_id: UUID = Field(default_factory=uuid4)
    action: str
    scope: dict[str, object]
    risk: str
    consequence: str
    required: bool = True


class AgentAssignment(BaseModel):
    assignment_id: UUID = Field(default_factory=uuid4)
    node: str
    agent: str
    dependencies: list[str] = Field(default_factory=list)
    status: AssignmentStatus
    reason: str
    approval: ApprovalCard | None = None
    attempt: int = 0
    failure_history: list[FailureRecord] = Field(default_factory=list)
    strategy_history: list[str] = Field(default_factory=list)


class SupervisorPlan(BaseModel):
    requirement_id: UUID
    requirement_version: int
    assignments: list[AgentAssignment]
    parallel_groups: list[list[str]]
    max_repair_rounds: int = Field(default=3, ge=1, le=10)
    repair_round: int = 0
    human_takeover_available: bool = True


class SupervisorAgent:
    async def plan_autonomously(
        self,
        requirement: RequirementSpec,
        *,
        project_id: str,
        available_tools: list[str],
        generator: CandidatePlanGenerator,
        validator: PlanValidator,
    ) -> CandidatePlan:
        """Generate, validate and select a plan; deterministic plan is fail-closed only."""
        try:
            generated = await generator.generate(
                requirement,
                project_id=project_id,
                available_tools=available_tools,
            )
            recommended = generated.candidates[generated.recommended_index]
            ordered = [recommended, *[item for item in generated.candidates if item != recommended]]
            for candidate in ordered:
                if validator.validate(candidate).valid:
                    return candidate
        except Exception:
            pass
        return safe_fallback_plan(requirement)

    def plan(
        self,
        requirement: RequirementSpec,
        outline: OutlinePlan,
        evidence: EvidenceBundle,
    ) -> SupervisorPlan:
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("Supervisor requires confirmed requirements")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        assignments: list[AgentAssignment] = []

        def add(
            node: str,
            agent: str,
            reason: str,
            dependencies: list[str],
            approval: ApprovalCard | None = None,
        ) -> None:
            assignments.append(
                AgentAssignment(
                    node=node,
                    agent=agent,
                    dependencies=dependencies,
                    status=(
                        AssignmentStatus.WAITING_APPROVAL if approval else AssignmentStatus.READY
                    ),
                    reason=reason,
                    approval=approval,
                )
            )

        if confirmed.requires_experiment:
            add(
                "experiment",
                "coding_agent",
                "已确认需求需要运行实验并取得真实结果",
                [],
                ApprovalCard(
                    action="execute_experiment",
                    scope={"requirement_id": str(confirmed.requirement_id)},
                    risk="将安装依赖并运行用户或第三方代码",
                    consequence="可能占用 CPU、GPU、磁盘和网络资源",
                ),
            )
        if confirmed.requires_generated_image:
            add(
                "image",
                "visual_agent",
                "已确认需求需要非数据生成图",
                [],
                ApprovalCard(
                    action="generate_image",
                    scope={"provider": "configured_image_provider"},
                    risk="可能调用付费图片 API",
                    consequence="提示词和获批素材将发送给所选 Provider",
                ),
            )
        if confirmed.requires_data_chart:
            add(
                "data_chart",
                "visual_agent",
                "需要根据真实数据生成图表",
                ["experiment"] if confirmed.requires_experiment else [],
            )
        add(
            "pre_review",
            "review_agent",
            "成文前检查框架和证据就绪度",
            [item.node for item in assignments],
        )
        add("writing", "writer_agent", "按已确认框架和 Evidence Pack 分章节写作", ["pre_review"])
        add("post_review", "review_agent", "定位事实、引用、结构和字数问题", ["writing"])
        add("repair", "repair_planner", "仅在审验失败时定向返修", ["post_review"])
        add("render", "render_agent", "审验通过后生成交付文件", ["post_review", "repair"])
        parallel = [
            [
                node
                for node in ("experiment", "image")
                if any(item.node == node for item in assignments)
            ]
        ]
        return SupervisorPlan(
            requirement_id=confirmed.requirement_id,
            requirement_version=confirmed.requirement_version,
            assignments=assignments,
            parallel_groups=[group for group in parallel if len(group) > 1],
        )

    @staticmethod
    def decide_approval(
        plan: SupervisorPlan, approval_id: UUID, *, approved: bool
    ) -> SupervisorPlan:
        updated = plan.model_copy(deep=True)
        assignment = next(
            (
                item
                for item in updated.assignments
                if item.approval and item.approval.approval_id == approval_id
            ),
            None,
        )
        if assignment is None:
            raise KeyError(approval_id)
        if assignment.status not in {
            AssignmentStatus.WAITING_APPROVAL,
            AssignmentStatus.READY,
            AssignmentStatus.CANCELLED,
        }:
            raise ValueError("approval can no longer change this assignment")
        assignment.status = AssignmentStatus.READY if approved else AssignmentStatus.CANCELLED
        return updated

    @staticmethod
    def replan_failure(
        plan: SupervisorPlan,
        node: str,
        failure: FailureRecord | None = None,
    ) -> SupervisorPlan:
        updated = plan.model_copy(deep=True)
        assignment = next((item for item in updated.assignments if item.node == node), None)
        if assignment is None:
            raise KeyError(node)
        assignment.attempt += 1
        if failure is not None:
            assignment.failure_history.append(failure)
            decision = RecoveryPlanner().decide(
                failure,
                prior_strategies=assignment.strategy_history,
            )
            assignment.strategy_history.append(decision.strategy)
            assignment.reason += f"; {decision.reason}; strategy={decision.strategy}"
            assignment.status = (
                AssignmentStatus.HUMAN_REQUIRED
                if decision.requires_human
                else AssignmentStatus.READY
            )
            return updated
        if assignment.attempt < 3:
            assignment.status = AssignmentStatus.READY
            assignment.reason += "; 前次执行失败, 将从最近 checkpoint 重试"
        else:
            assignment.status = AssignmentStatus.HUMAN_REQUIRED
            assignment.reason += "; 自动重试已用尽, 需要人工接管或修改计划"
        return updated

    @staticmethod
    def next_repair_round(plan: SupervisorPlan) -> SupervisorPlan:
        updated = plan.model_copy(deep=True)
        if updated.repair_round >= updated.max_repair_rounds:
            repair = next(item for item in updated.assignments if item.node == "repair")
            repair.status = AssignmentStatus.HUMAN_REQUIRED
            return updated
        updated.repair_round += 1
        return updated
