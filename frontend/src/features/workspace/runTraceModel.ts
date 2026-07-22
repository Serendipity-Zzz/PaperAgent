import type { RunTraceEvent, RunTraceRun, RunTraceStatus } from "./RunTraceCard";

export const TERMINAL_RUN_STATUSES = new Set<RunTraceStatus>([
  "completed",
  "failed",
  "cancelled",
  "superseded",
]);

const TERMINAL_LABELS: Record<Extract<RunTraceStatus, "completed" | "failed" | "cancelled" | "superseded">, string> = {
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  superseded: "已被修订替代",
};

export function runPhaseLabel(run: RunTraceRun): string {
  if (TERMINAL_RUN_STATUSES.has(run.status)) {
    return TERMINAL_LABELS[run.status as keyof typeof TERMINAL_LABELS];
  }
  if (run.status === "waiting_approval") return "等待用户审验";
  if (run.status === "paused" || run.current_phase === "recovery_required") return "等待恢复确认";
  const deliveryLabels: Record<string, string> = {
    resolving: "正在定位源文档版本",
    presentation_resolution: "正在解析封面与页眉页脚要求",
    presentation_revision: "正在创建封面与页眉页脚修订",
    layout: "正在协商目标格式版面能力",
    compilation: "正在生成目标文件",
    quality_assurance: "正在校验封面、页眉页脚与交付文件",
    binding: "正在校验并绑定图片资源",
    waiting: "正在等待资源完成",
    rendering: "正在生成目标文件",
    validating: "正在执行交付校验",
    repair_required: "需要定向修复",
    rejected: "交付校验未通过",
    delivered: "文件已校验并可下载",
  };
  return (run.current_phase && deliveryLabels[run.current_phase])
    || run.current_phase
    || (run.status === "pending" ? "排队中" : "正在处理");
}

export function publicEventSummary(event: RunTraceEvent | undefined): string {
  const raw = String(event?.data.summary ?? event?.data.phase ?? event?.type ?? "任务已持久化，等待新事件");
  return raw
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏凭据]")
    .replace(/[A-Za-z]:\\[^\s]+/g, "[本机路径已隐藏]")
    .slice(0, 240);
}

export function mergePublicRunEvents(
  previous: RunTraceEvent[], incoming: RunTraceEvent,
): RunTraceEvent[] {
  const byId = new Map(previous.map((event) => [event.id, event]));
  byId.set(incoming.id, incoming);
  return [...byId.values()].sort((left, right) => left.id - right.id).slice(-500);
}

export type DeliveryProgress = {
  targetFormat?: string;
  sourceRevision?: string;
  imageCount?: string;
  resumeNode?: string;
};

export function deliveryProgress(events: RunTraceEvent[]): DeliveryProgress {
  const progress: DeliveryProgress = {};
  for (const event of [...events].sort((left, right) => left.id - right.id)) {
    const data = event.data;
    if (typeof data.format === "string") progress.targetFormat = data.format.toUpperCase();
    if (typeof data.target_format === "string") progress.targetFormat = data.target_format.toUpperCase();
    if (typeof data.revision === "number" || typeof data.revision === "string") {
      progress.sourceRevision = `r${String(data.revision)}`;
    }
    const expected = typeof data.expected_images === "number" ? data.expected_images : undefined;
    const bound = typeof data.bound_images === "number" ? data.bound_images : undefined;
    if (expected !== undefined || bound !== undefined) {
      progress.imageCount = `${bound ?? 0}/${expected ?? "?"}`;
    }
    if (typeof data.resume_node === "string") progress.resumeNode = data.resume_node;
  }
  return progress;
}

export function publicFailureMessage(category: string, details: Record<string, unknown> = {}): string {
  const expected = Number(details.expected_images ?? 0);
  const bound = Number(details.bound_images ?? 0);
  const messages: Record<string, string> = {
    missing_revision: "没有找到唯一的源文档版本，请选择需要继续处理的版本。",
    ambiguous_revision: "检测到多个可能的源文档版本，请选择一个后从原任务继续。",
    missing_asset: `源报告要求 ${expected || "若干"} 张图，当前绑定 ${bound} 张；将从资源绑定阶段恢复，不会重新运行已成功的实验。`,
    pending_asset: "图片仍在生成或校验，任务会在资源绑定阶段继续等待。",
    ambiguous_asset: "发现多个同名图片候选，请选择正确资源后继续原任务。",
    derivative_failed: "图片格式转换失败，将仅重建受影响的图片衍生文件。",
    compile_error: "排版引擎编译失败，将保留正文和图片并只重试目标格式。",
    layout_error: "检测到裁切、溢出或异常分页，将只调整受影响的版面锚点。",
    presentation_ambiguity: "封面或页眉页脚指令存在歧义，请补充具体字段后从解析节点继续。",
    presentation_schema: "文档呈现字段不符合结构契约，将回到呈现解析节点修正。",
    presentation_field_missing: "生成文件缺少要求的封面或页眉页脚字段，将只重渲染受影响格式。",
    validation_error: "文件已生成但交付校验未通过，不会作为成功下载展示。",
  };
  return messages[category] ?? "任务需要恢复操作；已保留成功产物和最近安全断点。";
}
