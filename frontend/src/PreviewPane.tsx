import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

import { selectPreviewRenderer } from "./features/preview/registry";
import type { PreviewViewState } from "./features/preview/tabStore";
import { PresentationInspector } from "./features/presentation/PresentationInspector";
import { MarkdownContent } from "./shared/markdown/MarkdownContent";

export type PreviewAnchor = {
  source_file_id: string;
  source_hash: string;
  format: string;
  page?: number;
  sheet?: string;
  cell_range?: string;
  slide?: number;
  line_start?: number;
  line_end?: number;
  message_id?: string;
  json_path?: string;
  quote?: string;
};

export type PreviewPart = {
  index: number;
  kind: string;
  label: string;
  payload: { text?: string; html?: string; cells?: string[]; path?: string; size?: number };
  anchor: PreviewAnchor | null;
};

export type PreviewArtifact = {
  id: string;
  source_file_id: string;
  source_hash: string;
  source_name: string;
  media_type: string;
  status: string;
  fidelity: string;
  renderer: string;
  capabilities: string[];
  payload: Record<string, unknown>;
  part_count: number;
  error_message?: string;
};

type Props = {
  projectId: string;
  token: string;
  artifact: PreviewArtifact;
  parts: PreviewPart[];
  viewState: PreviewViewState;
  onViewStateChange: (patch: Partial<PreviewViewState>) => void;
  onStatus: (message: string) => void;
  onLoadMore: () => void;
};

type PdfCacheEntry = {
  promise: Promise<PDFDocumentProxy>;
  references: number;
  cleanup?: number;
};

const pdfCache = new Map<string, PdfCacheEntry>();

function acquirePdf(url: string, token: string): Promise<PDFDocumentProxy> {
  const key = `${url}:${token}`;
  const existing = pdfCache.get(key);
  if (existing) {
    existing.references += 1;
    if (existing.cleanup) window.clearTimeout(existing.cleanup);
    return existing.promise;
  }
  const promise = import("pdfjs-dist").then(({ GlobalWorkerOptions, getDocument }) => {
    GlobalWorkerOptions.workerSrc = workerUrl;
    return getDocument({ url, httpHeaders: { Authorization: `Bearer ${token}` } }).promise;
  });
  pdfCache.set(key, { promise, references: 1 });
  return promise;
}

function releasePdf(url: string, token: string): void {
  const key = `${url}:${token}`;
  const entry = pdfCache.get(key);
  if (!entry) return;
  entry.references = Math.max(0, entry.references - 1);
  if (entry.references) return;
  entry.cleanup = window.setTimeout(() => {
    if (entry.references) return;
    void entry.promise.then((document) => document.destroy()).catch(() => undefined);
    pdfCache.delete(key);
  }, 30_000);
}

function PdfCanvas({
  url,
  token,
  viewState,
  onViewStateChange,
  onError,
}: {
  url: string;
  token: string;
  viewState: PreviewViewState;
  onViewStateChange: (patch: Partial<PreviewViewState>) => void;
  onError?: (message: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pages, setPages] = useState(0);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [status, setStatus] = useState("正在加载 PDF…");

  useEffect(() => {
    let active = true;
    void acquirePdf(url, token)
      .then((loaded) => {
        if (!active) return;
        setDocument(loaded);
        setPages(loaded.numPages);
        setStatus("");
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "PDF 加载失败";
        setStatus(message);
        onError?.(message);
      });
    return () => {
      active = false;
      releasePdf(url, token);
    };
  }, [onError, token, url]);

  useEffect(() => {
    const target = container.current;
    if (!target) return undefined;
    const update = () => setSize({ width: target.clientWidth, height: target.clientHeight });
    update();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!document) return undefined;
    let cancelled = false;
    let renderTask: { cancel: () => void; promise: Promise<unknown> } | null = null;
    void document.getPage(Math.min(viewState.page, document.numPages)).then((pdfPage) => {
      if (cancelled) return;
      const natural = pdfPage.getViewport({ scale: 1 });
      const widthScale = Math.max(0.25, (size.width - 24) / natural.width);
      const pageScale = Math.min(widthScale, Math.max(0.25, (size.height - 64) / natural.height));
      const scale = viewState.fitMode === "width"
        ? widthScale
        : viewState.fitMode === "page"
          ? pageScale
          : viewState.fitMode === "actual"
            ? 1
            : viewState.zoom;
      const viewport = pdfPage.getViewport({ scale });
      const target = canvas.current;
      const context = target?.getContext("2d");
      if (!target || !context) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      target.width = Math.floor(viewport.width * ratio);
      target.height = Math.floor(viewport.height * ratio);
      target.style.width = `${viewport.width}px`;
      target.style.height = `${viewport.height}px`;
      renderTask = pdfPage.render({
        canvas: target,
        canvasContext: context,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
      });
      return renderTask.promise;
    }).catch((error: unknown) => {
      if (!cancelled && !(error instanceof Error && error.name === "RenderingCancelledException")) {
        setStatus(error instanceof Error ? error.message : "PDF 页面渲染失败");
      }
    });
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [document, size.height, size.width, viewState.fitMode, viewState.page, viewState.zoom]);

  return (
    <div className="pdf-viewer" ref={container}>
      <div className="page-control">
        <button type="button" disabled={viewState.page <= 1} onClick={() => onViewStateChange({ page: viewState.page - 1 })}>
          上一页
        </button>
        <span>{viewState.page} / {pages || "…"}</span>
        <button type="button" disabled={viewState.page >= pages} onClick={() => onViewStateChange({ page: viewState.page + 1 })}>
          下一页
        </button>
        <button type="button" aria-pressed={viewState.fitMode === "width"} onClick={() => onViewStateChange({ fitMode: "width" })}>适合宽度</button>
        <button type="button" aria-pressed={viewState.fitMode === "page"} onClick={() => onViewStateChange({ fitMode: "page" })}>适合页面</button>
        <button type="button" aria-pressed={viewState.fitMode === "actual"} onClick={() => onViewStateChange({ fitMode: "actual" })}>100%</button>
      </div>
      {status ? <p role={status.includes("失败") ? "alert" : "status"}>{status}</p> : null}
      <canvas ref={canvas} aria-label={`PDF 第 ${viewState.page} 页`} />
    </div>
  );
}

function AuthenticatedImage({ url, token, name }: { url: string; token: string; name: string }) {
  const [source, setSource] = useState("");
  useEffect(() => {
    let objectUrl = "";
    void fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((response) => response.blob())
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      });
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [token, url]);
  return source ? <img className="image-preview" src={source} alt={name} /> : <p>正在加载图片…</p>;
}

export function PreviewPane(props: Props) {
  const { artifact, parts, projectId, token } = props;
  const { query, zoom } = props.viewState;
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [officeFailed, setOfficeFailed] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [revisionDiff, setRevisionDiff] = useState<Array<{ id: string; kind: string; page: number }>>([]);
  const selected = parts.find((part) => part.index === selectedIndex) ?? parts[0] ?? null;
  const rendererKind = selectPreviewRenderer(artifact.media_type, artifact.source_name);
  const previewOptions = artifact.payload.options as
    | {
      artifact_id?: string;
      provenance?: { document_id?: string; revision_id?: string; document_revision?: number };
      raw_url?: string;
    }
    | undefined;
  const rawUrl = previewOptions?.raw_url
    ?? `/api/projects/${projectId}/files/${artifact.source_file_id}/raw`;
  const documentId = previewOptions?.provenance?.document_id;
  const documentRevision = previewOptions?.provenance?.document_revision;
  const sourceArtifactId = previewOptions?.artifact_id;
  const officePdfUrl = sourceArtifactId
    ? `/api/projects/${projectId}/artifacts/${sourceArtifactId}/preview-pdf`
    : "";
  const handleOfficeError = useCallback(() => setOfficeFailed(true), []);
  const visibleParts = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return parts;
    return parts.filter((part) => JSON.stringify(part.payload).toLocaleLowerCase().includes(normalized));
  }, [parts, query]);
  const markdownDocument = useMemo(
    () => visibleParts.map((part) => part.payload.text ?? "").join("\n\n"),
    [visibleParts],
  );

  useEffect(() => {
    const target = contentRef.current;
    if (target) target.scrollTop = props.viewState.scrollTop;
  }, [artifact.id, props.viewState.scrollTop]);

  async function openRaw(): Promise<void> {
    const response = await fetch(rawUrl, { headers: { Authorization: `Bearer ${token}` } });
    const objectUrl = URL.createObjectURL(await response.blob());
    window.open(objectUrl, "_blank", "noopener,noreferrer");
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  }

  async function selectionAction(action: "chat" | "evidence" | "citation"): Promise<void> {
    const text = window.getSelection()?.toString().trim() || selected?.payload.text || selected?.label;
    if (!text || !selected?.anchor) {
      props.onStatus("请先选择带定位信息的预览内容");
      return;
    }
    const response = await fetch(
      `/api/projects/${projectId}/preview/artifacts/${artifact.id}/selection`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ action, text, anchor: selected.anchor }),
      },
    );
    props.onStatus(response.ok ? `已生成可追溯的${action}上下文` : "操作失败");
  }

  async function annotate(): Promise<void> {
    if (!selected?.anchor) {
      props.onStatus("当前内容没有可批注锚点");
      return;
    }
    const body = window.prompt("输入批注意见");
    if (!body?.trim()) return;
    const response = await fetch(
      `/api/projects/${projectId}/preview/artifacts/${artifact.id}/annotations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ anchor: selected.anchor, body }),
      },
    );
    props.onStatus(response.ok ? "批注已保存并绑定来源锚点" : "批注保存失败");
  }

  async function reviseTypography(): Promise<void> {
    if (!selected?.anchor || !documentId) {
      props.onStatus("当前预览尚未绑定 Document IR，不能执行定向版式修改");
      return;
    }
    const body = window.prompt("描述字体或版式修改（只会修改当前定位块）");
    if (!body?.trim()) return;
    const response = await fetch(
      `/api/projects/${projectId}/documents/${documentId}/typography/from-annotation`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          anchor: selected.anchor,
          body,
          formats: ["md", "docx", "typst", "latex", "pdf"],
          allow_fallback: false,
        }),
      },
    );
    if (response.ok) {
      const result = await response.json() as {
        document: { revision: number };
        files: unknown[];
        visual_diff_files: Array<{ id: string; kind: string; page: number }>;
        render_errors: Record<string, string>;
      };
      setRevisionDiff(result.visual_diff_files);
      props.onStatus(
        `已完成局部版式修订 r${result.document.revision}，生成 ${result.files.length} 个真实产物${result.render_errors.pdf ? "；本机 PDF 引擎暂不可用" : ""}`,
      );
      return;
    }
    const error = await response.json() as { detail?: unknown };
    props.onStatus(`定向修改未执行：${JSON.stringify(error.detail ?? response.status)}`);
  }

  return (
    <div className="preview-pane">
      <div className="preview-meta">
        <strong>{artifact.source_name}</strong>
        <small>{artifact.renderer} · {artifact.fidelity} · {artifact.status} · {artifact.part_count} 个结构块</small>
        {artifact.error_message ? <p role="alert">降级原因：{artifact.error_message}</p> : null}
      </div>
      {documentId ? (
        <PresentationInspector
          key={`${documentId}:${documentRevision ?? "latest"}`}
          projectId={projectId}
          documentId={documentId}
          revision={documentRevision}
          token={token}
          onStatus={props.onStatus}
        />
      ) : null}
      <div className="preview-toolbar" aria-label="预览工具栏">
        {artifact.capabilities.includes("search") ? <input aria-label="在预览中搜索" placeholder="搜索" value={query} onChange={(event) => props.onViewStateChange({ query: event.target.value })} /> : null}
        {artifact.capabilities.includes("zoom") ? (
          <>
            <button type="button" aria-label="缩小预览" onClick={() => props.onViewStateChange({ zoom: Math.max(0.25, zoom - 0.1), fitMode: "custom" })}>−</button>
            <span>{Math.round(zoom * 100)}%</span>
            <button type="button" aria-label="放大预览" onClick={() => props.onViewStateChange({ zoom: Math.min(4, zoom + 0.1), fitMode: "custom" })}>＋</button>
          </>
        ) : null}
        {artifact.capabilities.includes("select") ? (
          <>
            <button type="button" onClick={() => void selectionAction("chat")}>发到对话</button>
            <button type="button" onClick={() => void selectionAction("evidence")}>加入证据</button>
            <button type="button" onClick={() => void selectionAction("citation")}>生成引用</button>
          </>
        ) : null}
        {artifact.capabilities.includes("annotate") ? <button type="button" onClick={() => void annotate()}>批注</button> : null}
        {documentId && selected?.anchor ? <button type="button" onClick={() => void reviseTypography()}>定向修改字体</button> : null}
        {artifact.capabilities.includes("system_open") ? <button type="button" onClick={() => void openRaw()}>系统打开</button> : null}
      </div>
      {revisionDiff.length ? (
        <section className="visual-diff-preview" aria-label="版式修改视觉差异">
          <strong>修改前 / 修改后 / 差异</strong>
          <div>
            {revisionDiff.map((item) => (
              <figure key={item.id}>
                <AuthenticatedImage
                  url={`/api/projects/${projectId}/files/${item.id}/raw`}
                  token={token}
                  name={`第 ${item.page} 页 ${item.kind}`}
                />
                <figcaption>第 {item.page} 页 · {item.kind}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      ) : null}
      <div
        className="preview-content"
        ref={contentRef}
        onScroll={(event) => props.onViewStateChange({ scrollTop: event.currentTarget.scrollTop })}
      >
        {rendererKind === "pdf" && artifact.status !== "failed" ? (
          <PdfCanvas
            url={rawUrl}
            token={token}
            viewState={props.viewState}
            onViewStateChange={props.onViewStateChange}
          />
        ) : rendererKind === "office" && officePdfUrl && !officeFailed ? (
          <PdfCanvas
            url={officePdfUrl}
            token={token}
            viewState={props.viewState}
            onViewStateChange={props.onViewStateChange}
            onError={handleOfficeError}
          />
        ) : rendererKind === "image" ? (
          <div style={{ width: `${zoom * 100}%` }}>
            <AuthenticatedImage url={rawUrl} token={token} name={artifact.source_name} />
          </div>
        ) : rendererKind === "markdown" && visibleParts.length ? (
          <div
            className="document-preview"
            onClick={() => setSelectedIndex(visibleParts[0]?.index ?? null)}
          >
            <MarkdownContent
              content={markdownDocument}
              renderLocalImage={sourceArtifactId ? (src, alt) => (
                <AuthenticatedImage
                  url={`/api/projects/${projectId}/artifacts/${sourceArtifactId}/asset?path=${encodeURIComponent(src)}`}
                  token={token}
                  name={alt}
                />
              ) : undefined}
            />
          </div>
        ) : rendererKind === "html" && visibleParts.length ? (
          <div className="sanitized-html-preview">
            {visibleParts.map((part) => (
              <iframe
                key={part.index}
                title={`${artifact.source_name} · ${part.label}`}
                sandbox=""
                srcDoc={part.payload.html ?? `<pre>${part.payload.text ?? ""}</pre>`}
              />
            ))}
          </div>
        ) : rendererKind === "code" && visibleParts.length ? (
          <ol className="code-preview" aria-label={`${artifact.source_name} 源代码`}>
            {visibleParts.flatMap((part) => (part.payload.text ?? "").split("\n")).map((line, index) => (
              <li key={`${index}:${line.slice(0, 12)}`}><code>{line || " "}</code></li>
            ))}
          </ol>
        ) : visibleParts.length ? (
          <div className={visibleParts[0]?.kind === "table_row" ? "structured-table" : "structured-preview"}>
            {rendererKind === "office" ? (
              <p className="preview-fallback" role="status">
                本机未提供 Word/LibreOffice 页面转换，当前显示结构预览，不冒充原版页面。
              </p>
            ) : null}
            {visibleParts.map((part) => (
              <button type="button" className={selected?.index === part.index ? "preview-part preview-part--selected" : "preview-part"} key={part.index} onClick={() => setSelectedIndex(part.index)}>
                <span>{part.label}</span>
                {part.payload.cells ? <code>{part.payload.cells.join(" | ")}</code> : null}
                {part.payload.text ? <pre>{part.payload.text}</pre> : null}
                {part.payload.path ? <code>{part.payload.path} · {part.payload.size} B</code> : null}
              </button>
            ))}
          </div>
        ) : (
          <div className="metadata-preview"><p>当前格式采用安全元数据预览。</p><pre>{JSON.stringify(artifact.payload, null, 2)}</pre></div>
        )}
        {parts.length < artifact.part_count ? <button type="button" className="load-more" onClick={props.onLoadMore}>加载更多</button> : null}
      </div>
    </div>
  );
}
