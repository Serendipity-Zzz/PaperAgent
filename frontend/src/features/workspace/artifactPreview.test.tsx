import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

import { App } from "../../App";

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function artifact(id: string, name: string) {
  return {
    id,
    link_id: `link-${id}`,
    kind: "output",
    mime_type: "text/markdown",
    original_name: name,
    sha256: "a".repeat(64),
    size_bytes: 128,
    run_id: "run-1",
    validation_status: "valid",
    relation: "output",
  };
}

describe("artifact cards and preview tabs", () => {
  afterEach(() => vi.unstubAllGlobals());

  beforeEach(() => {
    window.localStorage.clear();
    const rejected = {
      ...artifact("artifact-rejected", "broken.pdf"),
      delivery_status: "rejected",
    };
    const links = [artifact("artifact-1", "report.md"), artifact("artifact-2", "data.csv"), rejected];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "local-token" });
      if (url === "/api/projects") return json([{ id: "project-a", name: "项目 A" }]);
      if (url.endsWith("/files") || url.endsWith("/agent/tasks")) return json([]);
      if (url.includes("/conversations") && url.includes("/messages?")) {
        return json([{
          id: "message-1",
          session_id: "chat-a",
          role: "assistant",
          content: "文件已经准备完成。",
          created_at: "2026-01-01",
          artifact_links: links,
        }]);
      }
      if (url.endsWith("/conversations")) return json([{ id: "chat-a", title: "交付" }]);
      if (url.includes("/structured-preview")) {
        const id = url.includes("artifact-1") ? "artifact-1" : "artifact-2";
        const name = id === "artifact-1" ? "report.md" : "data.csv";
        return json({
          id: `preview-${id}`,
          source_file_id: id,
          source_hash: "a".repeat(64),
          source_name: name,
          media_type: name.endsWith(".md") ? "text/markdown" : "text/csv",
          status: "ready",
          fidelity: "structured",
          renderer: "text",
          capabilities: ["search", "select"],
          payload: { options: { raw_url: `/raw/${id}` } },
          part_count: 1,
        });
      }
      if (url.includes("/preview/artifacts/") && url.includes("/parts")) {
        return json({
          parts: [{ index: 0, kind: "text", label: "正文", payload: { text: "内容" } }],
        });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
  });

  it("opens multiple message artifacts in switchable closable tabs", async () => {
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelectorAll(".artifact-card__main")).toHaveLength(2), { timeout: 15_000 });
    const cards = container.querySelectorAll<HTMLButtonElement>(".artifact-card__main");
    expect(cards).toHaveLength(2);
    expect(screen.queryByText("broken.pdf")).not.toBeInTheDocument();

    fireEvent.click(cards[0]);
    const tabs = await screen.findByRole("navigation", { name: "已打开的预览文件" });
    expect(within(tabs).getByRole("button", { name: "report.md" })).toBeInTheDocument();

    fireEvent.click(cards[1]);
    expect(await within(tabs).findByRole("button", { name: "data.csv" })).toBeInTheDocument();
    fireEvent.click(within(tabs).getByRole("button", { name: "关闭 report.md" }));
    expect(within(tabs).queryByRole("button", { name: "report.md" })).not.toBeInTheDocument();
  }, 15_000);

  it("coalesces twenty concurrent opens into one request and one mounted tab", async () => {
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelectorAll(".artifact-card__main")).toHaveLength(2), { timeout: 15_000 });
    const first = container.querySelector<HTMLButtonElement>(".artifact-card__main");
    expect(first).not.toBeNull();

    for (let index = 0; index < 20; index += 1) fireEvent.click(first!);

    const tabs = await screen.findByRole("navigation", { name: "已打开的预览文件" });
    const previewCalls = vi.mocked(fetch).mock.calls.filter(([input]) => (
      String(input).includes("artifact-1/structured-preview")
    ));
    expect(previewCalls).toHaveLength(1);
    expect(within(tabs).getAllByRole("button", { name: "report.md" })).toHaveLength(1);
    await waitFor(() => {
      expect(container.querySelectorAll(".preview-tab-panel")).toHaveLength(1);
    }, { timeout: 15_000 });
  }, 15_000);
});
