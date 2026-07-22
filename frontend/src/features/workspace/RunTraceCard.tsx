import type { ReactNode } from "react";

import { deliveryProgress, publicEventSummary, runPhaseLabel } from "./runTraceModel";

export type RunTraceStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "superseded";

export type RunTraceRun = {
  id: string;
  status: RunTraceStatus;
  current_phase?: string;
  attempt?: number;
  started_at?: string | null;
  first_output_at?: string | null;
  finished_at?: string | null;
};

export type RunTraceEvent = { id: number; type: string; data: Record<string, unknown> };

type CommonProps = {
  run: RunTraceRun;
  events: RunTraceEvent[];
  elapsed: string;
  firstFeedback?: string | null;
  selected?: boolean;
  onSelect?: () => void;
  children?: ReactNode;
};

export function ActiveRunCard({
  run,
  events,
  elapsed,
  firstFeedback,
  selected,
  onSelect,
  children,
}: CommonProps) {
  const latest = events.at(-1);
  const delivery = deliveryProgress(events);
  return (
    <article
      className={`run-card run-card--compact ${selected ? "run-card--active" : ""}`}
      onClick={onSelect}
    >
      <div className="run-card__summary">
        <span className="run-spinner" aria-hidden="true" />
        <div>
          <strong>{runPhaseLabel(run)}</strong>
          <p>{publicEventSummary(latest)}</p>
        </div>
        <time>{elapsed}</time>
      </div>
      <div className="run-card__metrics">
        <span>状态 {run.status}</span>
        <span>尝试 {run.attempt ?? 0}</span>
        <span>事件 {events.length}</span>
        {firstFeedback ? <span>首个反馈 {firstFeedback}</span> : null}
        {delivery.targetFormat ? <span>格式 {delivery.targetFormat}</span> : null}
        {delivery.sourceRevision ? <span>来源 {delivery.sourceRevision}</span> : null}
        {delivery.imageCount ? <span>图片 {delivery.imageCount}</span> : null}
        {delivery.resumeNode ? <span>恢复点 {delivery.resumeNode}</span> : null}
      </div>
      {children}
      {events.length ? (
        <details onClick={(event) => event.stopPropagation()}>
          <summary>查看公开活动时间线</summary>
          <ol className="run-timeline">
            {events.slice(-12).map((event) => (
              <li key={event.id}>
                <code>{event.type}</code>
                <span>{publicEventSummary(event)}</span>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </article>
  );
}

export function HistoricalRunTrace({ run, events, elapsed, onSelect, children }: CommonProps) {
  return (
    <details className={`historical-run-trace historical-run-trace--${run.status}`}>
      <summary onClick={onSelect}>
        <span>{runPhaseLabel(run)} · 已处理 {elapsed}</span>
        <small>查看公开活动</small>
      </summary>
      <div className="historical-run-trace__body">
        <div className="run-card__metrics">
          <span>状态 {run.status}</span>
          <span>尝试 {run.attempt ?? 0}</span>
          <span>事件 {events.length}</span>
        </div>
        {events.length ? (
          <ol className="run-timeline">
            {events.slice(-12).map((event) => (
              <li key={event.id}>
                <code>{event.type}</code>
                <span>{publicEventSummary(event)}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="historical-run-trace__empty">完整时间线可在“活动”中查看。</p>
        )}
        {children}
      </div>
    </details>
  );
}
