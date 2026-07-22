from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from paperagent.providers.base import ProviderError
from paperagent.schemas.common import stable_json_hash


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CAPABILITY = "capability"
    INVALID_OUTPUT = "invalid_output"
    CONTEXT_OVERFLOW = "context_overflow"
    RESOURCE = "resource"
    DEPENDENCY = "dependency"
    CUDA = "cuda"
    CODE = "code"
    DATA = "data"
    RENDER = "render"
    QUALITY = "quality"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"
    POLICY = "policy"
    TERMINAL = "terminal"
    MISSING_REVISION = "missing_revision"
    AMBIGUOUS_REVISION = "ambiguous_revision"
    STRUCTURE_ERROR = "structure_error"
    MISSING_ASSET = "missing_asset"
    PENDING_ASSET = "pending_asset"
    AMBIGUOUS_ASSET = "ambiguous_asset"
    INVALID_ASSET = "invalid_asset"
    DERIVATIVE_FAILED = "derivative_failed"
    COMPILE_ERROR = "compile_error"
    LAYOUT_ERROR = "layout_error"
    VALIDATION_ERROR = "validation_error"
    PRESENTATION_AMBIGUITY = "presentation_ambiguity"
    PRESENTATION_SCHEMA = "presentation_schema"
    PRESENTATION_FIELD_MISSING = "presentation_field_missing"


class RecoveryAction(StrEnum):
    RETRY_BACKOFF = "retry_backoff"
    SWITCH_PROVIDER = "switch_provider"
    REPAIR_OUTPUT = "repair_output"
    COMPRESS_CONTEXT = "compress_context"
    SPLIT_TASK = "split_task"
    REDUCE_RESOURCE = "reduce_resource"
    REBUILD_ENVIRONMENT = "rebuild_environment"
    REPLAN = "replan"
    RECONCILE = "reconcile"
    REQUEST_INPUT = "request_input"
    HUMAN_TAKEOVER = "human_takeover"
    ABORT = "abort"
    WAIT = "wait"


class FailureRecord(BaseModel):
    node: str
    tool_name: str | None = None
    category: FailureCategory
    code: str
    message: str
    retryable: bool = False
    state_unknown: bool = False
    attempt: int = Field(default=1, ge=1)
    input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    document_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    responsible_node: str | None = None
    attempted_strategies: tuple[str, ...] = ()
    side_effect_status: str | None = None

    def fingerprint(self) -> str:
        return stable_json_hash(
            "|".join(
                (
                    self.node,
                    self.tool_name or "",
                    self.category.value,
                    self.code.strip().upper(),
                    self.normalized_error(),
                    self.document_id or "",
                    str(self.revision or ""),
                    self.input_hash or "",
                )
            )
        )

    def normalized_error(self) -> str:
        value = self.message.casefold()
        value = re.sub(r"[a-f0-9]{32,}", "<hash>", value)
        value = re.sub(r"\b\d+\b", "<n>", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value[:512]


class RecoveryDecision(BaseModel):
    action: RecoveryAction
    strategy: str
    reason: str
    retry_node: bool = False
    replan: bool = False
    requires_human: bool = False
    resume_node: str | None = None


class FailureAnalyzer:
    """Normalize technical and semantic failures without exposing credentials."""

    _PATTERNS: tuple[tuple[FailureCategory, re.Pattern[str]], ...] = (
        (
            FailureCategory.PRESENTATION_AMBIGUITY,
            re.compile(r"presentation.*ambigu|cover.*(?:value|field).*clarif", re.I),
        ),
        (
            FailureCategory.PRESENTATION_FIELD_MISSING,
            re.compile(
                r"(?:presentation|cover|header|footer).*(?:required|field|text).*(?:missing|absent)",
                re.I,
            ),
        ),
        (
            FailureCategory.PRESENTATION_SCHEMA,
            re.compile(r"presentation.*schema|page chrome.*unknown cover field", re.I),
        ),
        (
            FailureCategory.AMBIGUOUS_REVISION,
            re.compile(r"ambiguous.*revision|multiple.*revision", re.I),
        ),
        (
            FailureCategory.MISSING_REVISION,
            re.compile(r"missing.*revision|revision.*not found|no canonical revision", re.I),
        ),
        (
            FailureCategory.PENDING_ASSET,
            re.compile(r"asset.*pending|figure.*pending|barrier.*pending", re.I),
        ),
        (
            FailureCategory.AMBIGUOUS_ASSET,
            re.compile(r"ambiguous.*(?:asset|figure)|multiple.*(?:asset|figure)", re.I),
        ),
        (
            FailureCategory.INVALID_ASSET,
            re.compile(
                r"(?:asset|figure|image).*"
                r"(?:invalid|corrupt|hash mismatch|mime mismatch)|"
                r"(?:corrupt|hash mismatch|mime mismatch).*"
                r"(?:asset|figure|image)",
                re.I,
            ),
        ),
        (
            FailureCategory.MISSING_ASSET,
            re.compile(
                r"required (?:image|asset|figure).*(?:missing|not embedded)|"
                r"missing (?:asset|figure|image)",
                re.I,
            ),
        ),
        (FailureCategory.DERIVATIVE_FAILED, re.compile(r"derivative.*failed|asset deriv", re.I)),
        (
            FailureCategory.LAYOUT_ERROR,
            re.compile(r"layout|overflow|overfull|clipp|blank page", re.I),
        ),
        (
            FailureCategory.VALIDATION_ERROR,
            re.compile(r"delivery validation|validate_delivery|qa failed", re.I),
        ),
        (
            FailureCategory.COMPILE_ERROR,
            re.compile(r"xelatex|lualatex|tex compile|compile error", re.I),
        ),
        (
            FailureCategory.STRUCTURE_ERROR,
            re.compile(r"document.?ir|document structure|markdown.*leak|block tree", re.I),
        ),
        (
            FailureCategory.CONTEXT_OVERFLOW,
            re.compile(r"context.{0,20}(length|window)|maximum context|token limit", re.I),
        ),
        (
            FailureCategory.RATE_LIMIT,
            re.compile(r"rate.?limit|too many requests|\b429\b|quota", re.I),
        ),
        (
            FailureCategory.AUTHENTICATION,
            re.compile(r"invalid api.?key|authentication|unauthorized|\b401\b", re.I),
        ),
        (
            FailureCategory.PERMISSION,
            re.compile(r"permission|forbidden|access denied|\b403\b", re.I),
        ),
        (
            FailureCategory.CAPABILITY,
            re.compile(r"unsupported|capability|tool.{0,15}not|model.{0,15}not", re.I),
        ),
        (
            FailureCategory.INVALID_OUTPUT,
            re.compile(r"validation error|invalid json|schema|parse|malformed", re.I),
        ),
        (
            FailureCategory.RESOURCE,
            re.compile(r"out of memory|disk full|no space|resource exhausted", re.I),
        ),
        (
            FailureCategory.CUDA,
            re.compile(r"cuda|cudnn|nvcc|gpu driver|device-side assert", re.I),
        ),
        (
            FailureCategory.DEPENDENCY,
            re.compile(r"module not found|dependency|version conflict|cuda version|compiler", re.I),
        ),
        (
            FailureCategory.CODE,
            re.compile(r"traceback|syntaxerror|nameerror|typeerror|indexerror|keyerror", re.I),
        ),
        (
            FailureCategory.DATA,
            re.compile(
                r"file not found|missing (?:input|column|dataset)|empty dataset|data error",
                re.I,
            ),
        ),
        (
            FailureCategory.RENDER,
            re.compile(r"xelatex|typst|docx|fontspec|render(?:ing)? failed|pdf compile", re.I),
        ),
        (
            FailureCategory.SIDE_EFFECT_UNKNOWN,
            re.compile(r"state unknown|unknown outcome|connection lost after send", re.I),
        ),
    )

    @classmethod
    def analyze(
        cls,
        node: str,
        error: BaseException,
        *,
        attempt: int = 1,
        tool_name: str | None = None,
        input_hash: str | None = None,
        document_id: str | None = None,
        revision: int | None = None,
        responsible_node: str | None = None,
        attempted_strategies: list[str] | None = None,
        side_effect_status: str | None = None,
    ) -> FailureRecord:
        message = cls._safe_message(str(error))
        if isinstance(error, ProviderError):
            category = cls._provider_category(error, message)
            return FailureRecord(
                node=node,
                tool_name=tool_name,
                category=category,
                code=error.code,
                message=message,
                retryable=error.retryable,
                state_unknown=error.state_unknown,
                attempt=attempt,
                input_hash=input_hash,
                document_id=document_id,
                revision=revision,
                responsible_node=responsible_node,
                attempted_strategies=tuple(attempted_strategies or ()),
                side_effect_status=side_effect_status,
            )
        if isinstance(error, PermissionError):
            category = (
                FailureCategory.POLICY
                if re.search(r"deletion|outside the managed|blocked|not authorized", message, re.I)
                else FailureCategory.PERMISSION
            )
        elif isinstance(error, (TimeoutError, ConnectionError)):
            category = FailureCategory.TRANSIENT
        elif (classified := cls._from_message(message)) is not FailureCategory.TERMINAL:
            category = classified
        elif isinstance(error, (ValueError, TypeError, KeyError)):
            category = FailureCategory.INVALID_OUTPUT
        else:
            category = cls._from_message(message)
        return FailureRecord(
            node=node,
            tool_name=tool_name,
            category=category,
            code=error.__class__.__name__.upper(),
            message=message,
            retryable=category in {FailureCategory.TRANSIENT, FailureCategory.RATE_LIMIT},
            state_unknown=category is FailureCategory.SIDE_EFFECT_UNKNOWN,
            attempt=attempt,
            input_hash=input_hash,
            document_id=document_id,
            revision=revision,
            responsible_node=responsible_node,
            attempted_strategies=tuple(attempted_strategies or ()),
            side_effect_status=side_effect_status,
        )

    @classmethod
    def _provider_category(cls, error: ProviderError, message: str) -> FailureCategory:
        code = error.code.upper()
        if error.state_unknown:
            return FailureCategory.SIDE_EFFECT_UNKNOWN
        if code in {"RATE_LIMIT", "QUOTA_EXCEEDED"}:
            return FailureCategory.RATE_LIMIT
        if code in {"AUTHENTICATION", "INVALID_KEY"}:
            return FailureCategory.AUTHENTICATION
        if code in {"CAPABILITY_UNSUPPORTED", "NO_PROVIDER"}:
            return FailureCategory.CAPABILITY
        if code in {"INVALID_RESPONSE", "SCHEMA_ERROR"}:
            return FailureCategory.INVALID_OUTPUT
        if error.retryable:
            return FailureCategory.TRANSIENT
        return cls._from_message(message)

    @classmethod
    def _from_message(cls, message: str) -> FailureCategory:
        for category, pattern in cls._PATTERNS:
            if pattern.search(message):
                return category
        return FailureCategory.TERMINAL

    @staticmethod
    def _safe_message(message: str) -> str:
        redacted = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
        redacted = re.sub(
            r"(?i)(api[_ -]?key|authorization)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            redacted,
        )
        return redacted[:2_000]


class RecoveryPlanner:
    """Choose a materially different recovery strategy for non-transient failures."""

    def decide(
        self,
        failure: FailureRecord,
        *,
        prior_strategies: list[str] | None = None,
        max_strategy_attempts: int = 3,
    ) -> RecoveryDecision:
        prior = prior_strategies or []
        if len(prior) >= max_strategy_attempts:
            return RecoveryDecision(
                action=RecoveryAction.HUMAN_TAKEOVER,
                strategy="human_review_after_exhausted_strategies",
                reason="不同自动修复策略均未成功, 继续尝试可能重复消耗资源",
                requires_human=True,
            )
        category = failure.category
        delivery_routes: dict[FailureCategory, tuple[RecoveryAction, str, str, bool]] = {
            FailureCategory.PRESENTATION_AMBIGUITY: (
                RecoveryAction.REQUEST_INPUT,
                "clarify_presentation_requirement",
                "document_presentation_resolve",
                True,
            ),
            FailureCategory.PRESENTATION_SCHEMA: (
                RecoveryAction.REPAIR_OUTPUT,
                "resolve_presentation_schema_from_confirmed_requirement",
                "document_presentation_resolve",
                False,
            ),
            FailureCategory.PRESENTATION_FIELD_MISSING: (
                RecoveryAction.REPAIR_OUTPUT,
                "rerender_missing_presentation_field",
                "document_render",
                False,
            ),
            FailureCategory.MISSING_REVISION: (
                RecoveryAction.REQUEST_INPUT,
                "resolve_revision_or_request_target",
                "document_resolve_revision",
                True,
            ),
            FailureCategory.AMBIGUOUS_REVISION: (
                RecoveryAction.REQUEST_INPUT,
                "request_revision_choice",
                "document_resolve_revision",
                True,
            ),
            FailureCategory.STRUCTURE_ERROR: (
                RecoveryAction.REPAIR_OUTPUT,
                "normalize_invalid_document_blocks",
                "document_compose",
                False,
            ),
            FailureCategory.MISSING_ASSET: (
                RecoveryAction.REPLAN,
                "rebind_or_resume_responsible_asset_producer",
                "document_asset_barrier",
                False,
            ),
            FailureCategory.PENDING_ASSET: (
                RecoveryAction.WAIT,
                "await_pending_asset_barrier",
                "document_asset_barrier",
                False,
            ),
            FailureCategory.AMBIGUOUS_ASSET: (
                RecoveryAction.REQUEST_INPUT,
                "request_ambiguous_asset_choice",
                "document_asset_barrier",
                True,
            ),
            FailureCategory.INVALID_ASSET: (
                RecoveryAction.REPLAN,
                "replace_invalid_derivative_from_verified_source",
                "document_asset_barrier",
                False,
            ),
            FailureCategory.DERIVATIVE_FAILED: (
                RecoveryAction.REPAIR_OUTPUT,
                "derive_target_asset_again_from_verified_source",
                "document_asset_derive",
                False,
            ),
            FailureCategory.COMPILE_ERROR: (
                RecoveryAction.REPAIR_OUTPUT,
                "repair_classified_compile_diagnostics",
                "document_render",
                False,
            ),
            FailureCategory.LAYOUT_ERROR: (
                RecoveryAction.REPAIR_OUTPUT,
                "repair_affected_layout_anchor_only",
                "document_layout_resolve",
                False,
            ),
            FailureCategory.VALIDATION_ERROR: (
                RecoveryAction.REPLAN,
                "return_to_validator_responsible_node",
                failure.responsible_node or "document_validate_delivery",
                False,
            ),
        }
        if category is FailureCategory.LAYOUT_ERROR and "presentation" in failure.node:
            delivery_routes[FailureCategory.LAYOUT_ERROR] = (
                RecoveryAction.REPAIR_OUTPUT,
                "repair_presentation_layout_only",
                "document_presentation_layout",
                False,
            )
        if category in delivery_routes:
            action, strategy, resume_node, requires_human = delivery_routes[category]
            if strategy in prior:
                return RecoveryDecision(
                    action=RecoveryAction.HUMAN_TAKEOVER,
                    strategy="human_review_after_duplicate_delivery_strategy",
                    reason="相同故障指纹已使用过同一实质策略, 禁止机械重试",
                    requires_human=True,
                    resume_node=resume_node,
                )
            return RecoveryDecision(
                action=action,
                strategy=strategy,
                reason="按文档交付失败类别返回最小责任节点, 不重放完整链路",
                retry_node=action in {RecoveryAction.REPAIR_OUTPUT, RecoveryAction.WAIT},
                replan=action is RecoveryAction.REPLAN,
                requires_human=requires_human,
                resume_node=resume_node,
            )
        if category in {FailureCategory.TRANSIENT, FailureCategory.RATE_LIMIT}:
            return RecoveryDecision(
                action=RecoveryAction.RETRY_BACKOFF,
                strategy="exponential_backoff_with_jitter",
                reason="瞬时网络、服务繁忙或限流, 保持输入不变并退避重试",
                retry_node=True,
            )
        if category is FailureCategory.INVALID_OUTPUT:
            strategy = (
                "schema_repair_with_error_feedback"
                if "schema_repair_with_error_feedback" not in prior
                else "reduce_output_scope_and_split"
            )
            return RecoveryDecision(
                action=(
                    RecoveryAction.REPAIR_OUTPUT
                    if strategy.startswith("schema_repair")
                    else RecoveryAction.SPLIT_TASK
                ),
                strategy=strategy,
                reason="向模型反馈具体校验路径; 再次失败则缩小结构化输出范围",
                retry_node=True,
                replan=strategy == "reduce_output_scope_and_split",
            )
        if category is FailureCategory.CONTEXT_OVERFLOW:
            strategy = (
                "evidence_aware_context_compaction"
                if "evidence_aware_context_compaction" not in prior
                else "section_level_map_reduce"
            )
            return RecoveryDecision(
                action=(
                    RecoveryAction.COMPRESS_CONTEXT
                    if strategy.startswith("evidence_aware")
                    else RecoveryAction.SPLIT_TASK
                ),
                strategy=strategy,
                reason="先保留需求、证据定位和未完成任务进行压缩, 再按章节拆分",
                retry_node=True,
                replan=True,
            )
        if category is FailureCategory.CAPABILITY:
            return RecoveryDecision(
                action=RecoveryAction.SWITCH_PROVIDER,
                strategy="select_provider_by_required_capabilities",
                reason="当前模型缺少工具或结构化输出能力, 重新选择满足能力声明的 Provider",
                replan=True,
            )
        if category is FailureCategory.RESOURCE:
            return RecoveryDecision(
                action=RecoveryAction.REDUCE_RESOURCE,
                strategy="reduce_batch_resolution_or_model_size",
                reason="降低批量、分辨率或模型规模后重新评估本机可行性",
                replan=True,
            )
        if category is FailureCategory.DEPENDENCY:
            return RecoveryDecision(
                action=RecoveryAction.REBUILD_ENVIRONMENT,
                strategy="re_resolve_isolated_uv_environment",
                reason="重新扫描锁文件、CUDA 与依赖约束, 在隔离 uv 环境中修复",
                replan=True,
            )
        if category is FailureCategory.CUDA:
            return RecoveryDecision(
                action=RecoveryAction.REDUCE_RESOURCE,
                strategy="select_compatible_cuda_or_cpu_variant",
                reason="根据驱动、CUDA 与显存重新选择兼容构建; 不可行时切换 CPU 或更小模型",
                replan=True,
            )
        if category is FailureCategory.CODE:
            return RecoveryDecision(
                action=RecoveryAction.REPAIR_OUTPUT,
                strategy="targeted_source_revision_from_traceback",
                reason="把 traceback、源码 hash 和失败行反馈给 CodingAgent, 生成新源码 revision",
                retry_node=True,
                replan=True,
            )
        if category is FailureCategory.DATA:
            return RecoveryDecision(
                action=RecoveryAction.REQUEST_INPUT,
                strategy="validate_or_request_missing_data_artifact",
                reason="先检查输入 Artifact、字段和数据完整性, 缺少真实数据时请求用户补充",
                requires_human=True,
            )
        if category is FailureCategory.RENDER:
            return RecoveryDecision(
                action=RecoveryAction.REPAIR_OUTPUT,
                strategy="targeted_renderer_font_or_layout_repair",
                reason="根据渲染日志只修改字体、公式、图片或页面布局的受影响部分",
                retry_node=True,
            )
        if category is FailureCategory.QUALITY:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                strategy="targeted_repair_from_review_issues",
                reason="按结构化 Review Issue 定位责任 Agent 和最小重跑范围",
                replan=True,
            )
        if category is FailureCategory.SIDE_EFFECT_UNKNOWN:
            return RecoveryDecision(
                action=RecoveryAction.RECONCILE,
                strategy="query_idempotency_record_before_retry",
                reason="副作用结果未知, 必须先按请求 ID 或产物状态对账, 禁止直接重发",
                requires_human=True,
            )
        if category in {
            FailureCategory.AUTHENTICATION,
            FailureCategory.PERMISSION,
            FailureCategory.POLICY,
        }:
            return RecoveryDecision(
                action=RecoveryAction.REQUEST_INPUT,
                strategy="request_user_authorization_or_configuration",
                reason="凭据、权限或策略问题不能通过重复调用解决",
                requires_human=True,
            )
        return RecoveryDecision(
            action=RecoveryAction.ABORT,
            strategy="fail_closed_with_diagnostics",
            reason="未找到安全且有实质差异的自动恢复策略",
            requires_human=True,
        )


def retryable_by_langgraph(error: Exception) -> bool:
    failure = FailureAnalyzer.analyze("unknown", error)
    return failure.category in {FailureCategory.TRANSIENT, FailureCategory.RATE_LIMIT}
