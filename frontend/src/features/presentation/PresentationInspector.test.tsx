import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

import { PresentationInspector } from "./PresentationInspector";

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function presentation(revision = 1, school = "合成大学甲") {
  return {
    document_id: "doc-1",
    revision,
    revision_id: `doc-1:r${revision}`,
    presentation_hash: String(revision).repeat(64),
    content_hash: "c".repeat(64),
    asset_set_hash: "a".repeat(64),
    presentation: {
      cover: {
        enabled: true,
        title: "驻波实验报告",
        subtitle: null,
        fields: [
          { semantic_key: "author", label: "姓名", value: "合成用户甲", order: 10 },
          { semantic_key: "institution", label: "学校", value: school, order: 20 },
        ],
      },
      page_chrome: {
        default: {
          header: {
            left: [],
            center: [{ kind: "document_title" }],
            right: [],
          },
          footer: {
            left: [],
            center: [
              { kind: "page_number" },
              { kind: "text", text: "/" },
              { kind: "total_pages" },
            ],
            right: [],
          },
        },
        different_first_page: true,
        different_odd_even: false,
      },
    },
    diagnostics: {
      md: { cover: "semantic", page_chrome: "preview_only" },
      docx: { cover: "native", page_chrome: "native" },
      pdf: { cover: "native", page_chrome: "native" },
    },
    impact: {
      rewrites_content: false,
      reruns_experiment: false,
      reruns_assets: false,
    },
  };
}

describe("structured document presentation editor", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("edits the canonical revision and emits structured token operations", async () => {
    const status = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") {
        return json({ summary: presentation(2, "合成大学乙"), artifacts: [], render_errors: {} });
      }
      if (url.includes("presentation-preview")) {
        return json({ revision: url.includes("revision=2") ? 2 : 1, html: "<!doctype html><p>封面</p>" });
      }
      return json(presentation());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <PresentationInspector
        projectId="project-a"
        documentId="doc-1"
        revision={1}
        token="local-token"
        onStatus={status}
      />,
    );

    const summary = await screen.findByText(/文档呈现/);
    const details = summary.closest("details");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(summary.closest("summary")!);
    expect(await screen.findByTitle("结构化封面分页预览")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "精确编辑封面与页眉页脚" }));

    fireEvent.change(screen.getByLabelText("字段 2 内容"), { target: { value: "合成大学乙" } });
    const footerCenter = screen.getByLabelText("页脚中");
    fireEvent.change(footerCenter, { target: { value: "第 {page}/{pages} 页" } });
    fireEvent.click(screen.getByRole("button", { name: "保存定向修改" }));

    await waitFor(() => expect(status).toHaveBeenCalledWith(expect.stringContaining("r2")));
    expect(status.mock.calls.at(-1)?.[0]).not.toContain("合成大学乙");
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post).toBeDefined();
    const payload = JSON.parse(String(post?.[1]?.body)) as {
      revision: number;
      operations: Array<Record<string, unknown>>;
    };
    expect(payload.revision).toBe(1);
    expect(payload.operations).toContainEqual(expect.objectContaining({
      kind: "upsert_cover_field",
      semantic_key: "institution",
      value: "合成大学乙",
    }));
    expect(payload.operations).toContainEqual(expect.objectContaining({
      kind: "set_footer_region",
      region: "center",
      tokens: [
        { kind: "text", value: "第 " },
        { kind: "page_number" },
        { kind: "text", value: "/" },
        { kind: "total_pages" },
        { kind: "text", value: " 页" },
      ],
    }));
    expect(screen.getByText(/文档呈现/).closest("summary")).toHaveTextContent("r2");
  });

  it("rolls back local fields after a revision conflict without leaking values", async () => {
    const status = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return json({ detail: { code: "PRESENTATION_REVISION_CONFLICT" } }, 409);
      }
      if (String(input).includes("presentation-preview")) {
        return json({ revision: 1, html: "<!doctype html><p>封面</p>" });
      }
      return json(presentation());
    }));

    render(
      <PresentationInspector
        projectId="project-a"
        documentId="doc-1"
        revision={1}
        token="local-token"
        onStatus={status}
      />,
    );
    fireEvent.click((await screen.findByText(/文档呈现/)).closest("summary")!);
    fireEvent.click(screen.getByRole("button", { name: "精确编辑封面与页眉页脚" }));
    fireEvent.change(screen.getByLabelText("字段 2 内容"), { target: { value: "不应保留的值" } });
    fireEvent.click(screen.getByRole("button", { name: "保存定向修改" }));

    await waitFor(() => expect(status).toHaveBeenCalledWith(expect.stringContaining("PRESENTATION_REVISION_CONFLICT")));
    expect(screen.getByLabelText("字段 2 内容")).toHaveValue("合成大学甲");
    expect(status.mock.calls.at(-1)?.[0]).not.toContain("不应保留的值");
  });
});
