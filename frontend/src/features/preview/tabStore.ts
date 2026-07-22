import type { PreviewArtifact, PreviewPart } from "../../PreviewPane";

export type PreviewViewState = {
  query: string;
  zoom: number;
  page: number;
  fitMode: "width" | "page" | "actual" | "custom";
  scrollTop: number;
};

export type PreviewTabState<TDelivery = unknown> = {
  id: string;
  title: string;
  artifact: PreviewArtifact;
  parts: PreviewPart[];
  delivery?: TDelivery;
  viewState: PreviewViewState;
};

export const DEFAULT_PREVIEW_VIEW_STATE: PreviewViewState = {
  query: "",
  zoom: 1,
  page: 1,
  fitMode: "width",
  scrollTop: 0,
};

export function previewTabKey(
  projectId: string,
  artifactId: string,
  revisionId?: string | null,
): string {
  return `${projectId}:${artifactId}:${revisionId || "latest"}`;
}

export function upsertPreviewTab<T>(
  current: PreviewTabState<T>[],
  incoming: PreviewTabState<T>,
): PreviewTabState<T>[] {
  const index = current.findIndex((item) => item.id === incoming.id);
  if (index < 0) return [...current, incoming];
  const next = [...current];
  next[index] = { ...incoming, viewState: current[index].viewState };
  return next;
}
