import { useEffect, useMemo, useState } from "react";

type PresentationToken = {
  kind: "text" | "document_title" | "cover_field" | "current_heading" | "page_number" | "total_pages" | "date";
  text?: string;
  value?: string;
  field_key?: string;
};

type RegionLine = {
  left: PresentationToken[];
  center: PresentationToken[];
  right: PresentationToken[];
};

type PresentationSummary = {
  document_id: string;
  revision: number;
  revision_id: string;
  presentation_hash: string;
  presentation: {
    cover: {
      enabled: boolean;
      title: string;
      subtitle: string | null;
      fields: Array<{
        semantic_key: string;
        label: string;
        value: string;
        order: number;
      }>;
    };
    page_chrome: {
      default: { header: RegionLine; footer: RegionLine };
      different_first_page: boolean;
      different_odd_even: boolean;
    };
  };
  diagnostics: Record<string, { cover: string; page_chrome: string }>;
  impact: {
    rewrites_content: boolean;
    reruns_experiment: boolean;
    reruns_assets: boolean;
  };
};

type EditableField = PresentationSummary["presentation"]["cover"]["fields"][number];
type PatchOperation = Record<string, unknown>;

type Props = {
  projectId: string;
  documentId: string;
  revision?: number;
  token: string;
  onStatus: (message: string) => void;
};

const REGION_PLACEHOLDERS: Record<string, PresentationToken["kind"]> = {
  "{title}": "document_title",
  "{heading}": "current_heading",
  "{page}": "page_number",
  "{pages}": "total_pages",
  "{date}": "date",
};

function tokenText(token: PresentationToken): string {
  if (token.kind === "text") return token.text ?? token.value ?? "";
  if (token.kind === "cover_field") return `{field:${token.field_key ?? ""}}`;
  return Object.entries(REGION_PLACEHOLDERS).find(([, kind]) => kind === token.kind)?.[0] ?? "";
}

function parseTokens(value: string): PresentationToken[] {
  const pattern = /(\{(?:title|heading|page|pages|date)\}|\{field:[a-z][a-z0-9_.-]*\})/g;
  const result: PresentationToken[] = [];
  let cursor = 0;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) result.push({ kind: "text", value: value.slice(cursor, index) });
    const raw = match[0];
    if (raw.startsWith("{field:")) {
      result.push({ kind: "cover_field", field_key: raw.slice(7, -1) });
    } else {
      result.push({ kind: REGION_PLACEHOLDERS[raw] });
    }
    cursor = index + raw.length;
  }
  if (cursor < value.length) result.push({ kind: "text", value: value.slice(cursor) });
  return result.filter((item) => item.kind !== "text" || Boolean(item.value));
}

function lineText(tokens: PresentationToken[]): string {
  return tokens.map(tokenText).join("");
}

function headers(token: string): HeadersInit {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

export function PresentationInspector(props: Props) {
  const { documentId, onStatus, projectId, revision, token } = props;
  const [summary, setSummary] = useState<PresentationSummary | null>(null);
  const [previewHtml, setPreviewHtml] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fields, setFields] = useState<EditableField[]>([]);
  const [title, setTitle] = useState("");
  const [subtitle, setSubtitle] = useState("");
  const [regions, setRegions] = useState<Record<string, string>>({});
  const [differentFirst, setDifferentFirst] = useState(true);
  const [differentOddEven, setDifferentOddEven] = useState(false);
  const [formats, setFormats] = useState<string[]>([]);

  const revisionQuery = revision ? `?revision=${revision}` : "";

  function normalize(next: PresentationSummary): void {
    setSummary(next);
    setFields(next.presentation.cover.fields.map((item) => ({ ...item })));
    setTitle(next.presentation.cover.title);
    setSubtitle(next.presentation.cover.subtitle ?? "");
    const chrome = next.presentation.page_chrome.default;
    setRegions({
      header_left: lineText(chrome.header.left),
      header_center: lineText(chrome.header.center),
      header_right: lineText(chrome.header.right),
      footer_left: lineText(chrome.footer.left),
      footer_center: lineText(chrome.footer.center),
      footer_right: lineText(chrome.footer.right),
    });
    setDifferentFirst(next.presentation.page_chrome.different_first_page);
    setDifferentOddEven(next.presentation.page_chrome.different_odd_even);
  }

  useEffect(() => {
    let active = true;
    void Promise.all([
      fetch(
        `/api/projects/${projectId}/documents/${documentId}/presentation${revisionQuery}`,
        { headers: headers(token) },
      ),
      fetch(
        `/api/projects/${projectId}/documents/${documentId}/presentation-preview${revisionQuery}`,
        { headers: headers(token) },
      ),
    ]).then(async ([summaryResponse, previewResponse]) => {
      if (!summaryResponse.ok || !previewResponse.ok) throw new Error("presentation unavailable");
      const next = await summaryResponse.json() as PresentationSummary;
      const preview = await previewResponse.json() as { html: string };
      if (!active) return;
      normalize(next);
      setPreviewHtml(preview.html);
    }).catch(() => {
      if (active) onStatus("当前文件没有可编辑的文档呈现信息");
    });
    return () => { active = false; };
  }, [documentId, onStatus, projectId, revisionQuery, token]);

  const originalKeys = useMemo(
    () => new Set(summary?.presentation.cover.fields.map((item) => item.semantic_key) ?? []),
    [summary],
  );

  function updateField(index: number, patch: Partial<EditableField>): void {
    setFields((current) => current.map((item, position) => (
      position === index ? { ...item, ...patch } : item
    )));
  }

  function moveField(index: number, offset: number): void {
    setFields((current) => {
      const target = index + offset;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  function addField(): void {
    const suffix = fields.length + 1;
    setFields((current) => [
      ...current,
      {
        semantic_key: `custom.field_${suffix}`,
        label: `自定义字段 ${suffix}`,
        value: "待填写",
        order: (current.length + 1) * 10,
      },
    ]);
  }

  async function reloadPreview(revision: number): Promise<void> {
    const response = await fetch(
      `/api/projects/${props.projectId}/documents/${props.documentId}/presentation-preview?revision=${revision}`,
      { headers: headers(props.token) },
    );
    if (response.ok) setPreviewHtml(((await response.json()) as { html: string }).html);
  }

  async function save(): Promise<void> {
    if (!summary || saving) return;
    const rollback = summary;
    const operations: PatchOperation[] = [
      { kind: "set_cover_title", value: title.trim() || null },
      { kind: "set_cover_subtitle", value: subtitle.trim() || null },
      ...fields.map((item, index) => ({
        kind: "upsert_cover_field",
        semantic_key: item.semantic_key,
        label: item.label.trim(),
        value: item.value.trim(),
        order: (index + 1) * 10,
      })),
      ...[...originalKeys]
        .filter((key) => !fields.some((item) => item.semantic_key === key))
        .map((semantic_key) => ({ kind: "remove_cover_field", semantic_key })),
      {
        kind: "reorder_cover_fields",
        ordered_keys: fields.map((item) => item.semantic_key),
      },
      ...(["left", "center", "right"] as const).flatMap((region) => [
        {
          kind: "set_header_region",
          region,
          tokens: parseTokens(regions[`header_${region}`] ?? ""),
        },
        {
          kind: "set_footer_region",
          region,
          tokens: parseTokens(regions[`footer_${region}`] ?? ""),
        },
      ]),
      { kind: "set_first_page_policy", bool_value: differentFirst },
      { kind: "set_odd_even_policy", bool_value: differentOddEven },
    ];
    setSaving(true);
    try {
      const response = await fetch(
        `/api/projects/${props.projectId}/documents/${props.documentId}/presentation/patch`,
        {
          method: "POST",
          headers: headers(props.token),
          body: JSON.stringify({ revision: summary.revision, operations, formats }),
        },
      );
      if (!response.ok) {
        const detail = await response.json() as { detail?: { code?: string } };
        throw new Error(detail.detail?.code ?? `HTTP_${response.status}`);
      }
      const result = await response.json() as {
        summary: PresentationSummary;
        artifacts: unknown[];
        render_errors?: Record<string, { code: string; action: string }>;
      };
      normalize(result.summary);
      await reloadPreview(result.summary.revision);
      setEditing(false);
      const failedFormats = Object.keys(result.render_errors ?? {});
      props.onStatus(failedFormats.length
        ? `文档呈现已更新到 r${result.summary.revision}；${failedFormats.join("/").toUpperCase()} 渲染失败，可单独重试渲染，正文、实验和图片均未重跑`
        : `文档呈现已更新到 r${result.summary.revision}；正文、实验和图片均未重跑${result.artifacts.length ? `，新生成 ${result.artifacts.length} 个文件` : ""}`);
    } catch (error) {
      normalize(rollback);
      props.onStatus(`文档呈现修改失败，已回滚界面：${String(error)}`);
    } finally {
      setSaving(false);
    }
  }

  if (!summary) return null;

  return (
    <details className="presentation-inspector">
      <summary>
        <span><strong>文档呈现</strong> · r{summary.revision}</span>
        <small>{fields.length} 个封面字段 · 页眉页脚 {differentFirst ? "封面隐藏" : "首页显示"}</small>
      </summary>
      <div className="presentation-summary-grid">
        <span>封面：{summary.presentation.cover.enabled ? "启用" : "关闭"}</span>
        <span>DOCX/PDF：原生页眉页脚</span>
        <span>Markdown：语义封面，不伪造物理分页</span>
      </div>
      <p className="presentation-impact">定向修改只创建 presentation revision，不重写正文、不重跑实验、不重新生成图片。</p>
      {previewHtml ? (
        <iframe
          className="presentation-cover-preview"
          title="结构化封面分页预览"
          sandbox=""
          srcDoc={previewHtml}
        />
      ) : null}
      <button type="button" onClick={() => setEditing((value) => !value)}>
        {editing ? "收起精确编辑" : "精确编辑封面与页眉页脚"}
      </button>
      {editing ? (
        <div className="presentation-editor" aria-label="文档呈现精确编辑">
          <label>封面标题<input value={title} onChange={(event) => setTitle(event.target.value)} /></label>
          <label>副标题<input value={subtitle} onChange={(event) => setSubtitle(event.target.value)} /></label>
          <div className="presentation-fields">
            {fields.map((item, index) => (
              <div className="presentation-field-row" key={item.semantic_key}>
                <input aria-label={`字段 ${index + 1} 标签`} value={item.label} onChange={(event) => updateField(index, { label: event.target.value })} />
                <input aria-label={`字段 ${index + 1} 内容`} value={item.value} onChange={(event) => updateField(index, { value: event.target.value })} />
                <button type="button" aria-label="上移字段" onClick={() => moveField(index, -1)}>↑</button>
                <button type="button" aria-label="下移字段" onClick={() => moveField(index, 1)}>↓</button>
                <button type="button" onClick={() => setFields((current) => current.filter((_, position) => position !== index))}>删除</button>
              </div>
            ))}
            <button type="button" onClick={addField}>添加自定义字段</button>
          </div>
          <p className="presentation-token-help">页眉页脚可使用 {"{title}"}、{"{heading}"}、{"{page}"}、{"{pages}"}、{"{date}"}、{"{field:author}"}。</p>
          <div className="presentation-regions">
            {(["header", "footer"] as const).flatMap((area) => (
              ["left", "center", "right"] as const
            ).map((region) => {
              const key = `${area}_${region}`;
              return (
                <label key={key}>{area === "header" ? "页眉" : "页脚"}{region === "left" ? "左" : region === "center" ? "中" : "右"}
                  <input value={regions[key] ?? ""} onChange={(event) => setRegions((current) => ({ ...current, [key]: event.target.value }))} />
                </label>
              );
            }))}
          </div>
          <label className="presentation-check"><input type="checkbox" checked={differentFirst} onChange={(event) => setDifferentFirst(event.target.checked)} />封面使用独立页眉页脚（默认隐藏）</label>
          <label className="presentation-check"><input type="checkbox" checked={differentOddEven} onChange={(event) => setDifferentOddEven(event.target.checked)} />启用奇偶页差异</label>
          <fieldset><legend>同时重渲染（可选）</legend>
            {(["md", "docx", "pdf"] as const).map((format) => (
              <label className="presentation-check" key={format}><input type="checkbox" checked={formats.includes(format)} onChange={(event) => setFormats((current) => event.target.checked ? [...current, format] : current.filter((item) => item !== format))} />{format.toUpperCase()}</label>
            ))}
          </fieldset>
          <div className="presentation-editor-actions">
            <button type="button" disabled={saving} onClick={() => void save()}>{saving ? "正在校验并渲染…" : "保存定向修改"}</button>
            <button type="button" disabled={saving} onClick={() => { normalize(summary); setEditing(false); }}>取消</button>
          </div>
        </div>
      ) : null}
    </details>
  );
}
