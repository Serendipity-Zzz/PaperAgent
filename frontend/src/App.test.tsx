import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

import { App } from "./App";

describe("PaperAgent shell", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("opens and closes the preview panel", () => {
    render(<App />);
    const workspace = screen.getByRole("main");
    fireEvent.click(screen.getByRole("button", { name: "打开预览" }));
    expect(workspace).toHaveAttribute("data-preview-open", "true");
    fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
    expect(workspace).toHaveAttribute("data-preview-open", "false");
  });

  it("keeps the workspace toolbar in one non-wrapping action group", () => {
    const { container } = render(<App />);
    const topbar = container.querySelector(".topbar");
    const actions = container.querySelector(".topbar-actions");
    expect(topbar).toBeInTheDocument();
    expect(actions).toBeInTheDocument();
    expect(actions).toContainElement(screen.getByRole("button", { name: "打开预览" }));
    expect(actions).toContainElement(screen.getByRole("button", { name: "恢复中心" }));
  });

  it("collapses the sidebar and exposes keyboard-resizable pane separators", () => {
    const { container } = render(<App />);
    const workspace = screen.getByRole("main");
    fireEvent.keyDown(screen.getByRole("separator", { name: "调整左侧栏宽度" }), {
      key: "ArrowRight",
    });
    expect(workspace.getAttribute("style")).toContain("270px");
    fireEvent.click(screen.getByRole("button", { name: "隐藏左侧栏" }));
    expect(workspace).toHaveAttribute("data-sidebar-collapsed", "true");
    fireEvent.mouseEnter(container.querySelector(".sidebar-peek-zone") as Element);
    expect(workspace).toHaveAttribute("data-sidebar-peek", "true");
    fireEvent.click(screen.getByRole("button", { name: "显示左侧栏" }));
    expect(workspace).toHaveAttribute("data-sidebar-collapsed", "false");
    expect(screen.getByRole("separator", { name: "调整右侧预览宽度" })).toBeInTheDocument();
  });

  it("separates active, saved, new text and new image provider zones", async () => {
    const saved = [{
      id: "saved-text",
      display_name: "Saved text",
      modality: "text",
      protocol: "openai_compatible",
      provider_type: "openai_compatible",
      base_url: "https://provider.test/v1",
      model: "model-a",
      model_name: "model-a",
      capabilities: ["chat"],
      has_credential: true,
      credential_status: "available",
      enabled: true,
      active: true,
      binding_id: "global:*:text",
      health_status: "healthy",
      health_detail: "probe passed",
      version: 2,
      secret_version: 1,
    }];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const body = url.endsWith("/api/bootstrap-token")
        ? { token: "test-token" }
        : url.endsWith("/api/settings/providers")
          ? saved
          : url.endsWith("/api/settings/provider-bindings")
            ? [{ id: "global:*:text", scope: "global", modality: "text", provider_id: "saved-text", version: 1 }]
            : url.endsWith("/api/runtime/resources")
              ? { limits: { remote_llm: 4, document_write: 1, cpu_job: 2, gpu_job: 1, image_generation: 2, render: 1 }, used: {}, queued: [] }
              : [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    await waitFor(() => expect(screen.getAllByText("Saved text")).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "当前模型" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已保存 Provider" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "新增文本 Provider" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "新增生图 Provider" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("例如 main-writing-model")).toHaveValue("");
    expect(screen.getByLabelText("替换 saved-text 的 API Key")).toHaveValue("");
  });
});
