import { render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

import { App } from "../../App";

function json(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const running = {
  id: "run-1",
  kind: "agent.turn",
  status: "running",
  current_phase: "understanding",
  attempt: 1,
  started_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:01Z",
  payload: { session_id: "chat-1", content: "write a report" },
};

describe("durable run progress", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "local-token" });
      if (url === "/api/projects") return json([{ id: "project-1", name: "论文项目" }]);
      if (url.endsWith("/files")) return json([]);
      if (url.endsWith("/conversations")) return json([{ id: "chat-1", title: "主会话" }]);
      if (url.includes("/messages?")) return json([]);
      if (url.endsWith("/agent/tasks")) return json([running]);
      if (url.endsWith("/agent/tasks/run-1")) return json(running);
      if (url.endsWith("/agent/tasks/run-1/inspect")) {
        return json({ task: running, events: [], approvals: [] });
      }
      if (url === "/api/recovery?project_id=project-1&task_id=run-1") {
        return json({ completed: 0, running: [], unknown: [], pending: [], stopped_at: null, requires_attention: false });
      }
      if (url.includes("/runs/run-1/events/stream")) {
        return new Response(
          'id: 1\nevent: run.progress\ndata: {"phase":"understanding","summary":"正在解析需求"}\n\n',
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      throw new Error(`unexpected request: ${url}`);
    }));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("shows public phase, timer metrics, and replayed events", async () => {
    const view = render(<App />);
    expect(await screen.findByText("understanding")).toBeInTheDocument();
    expect((await screen.findAllByText("正在解析需求")).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByText("事件 1")).toBeInTheDocument());
    expect(screen.getByText("尝试 1")).toBeInTheDocument();
    expect(screen.getByText("查看公开活动时间线")).toBeInTheDocument();
    const pause = screen.getByRole("button", { name: "暂停" });
    const cancel = screen.getByRole("button", { name: "取消" });
    expect(pause.parentElement).toHaveClass("composer-actions");
    expect(cancel.parentElement).toBe(pause.parentElement);
    view.unmount();
  });

  it("loads recovery records only for the active task", async () => {
    const view = render(<App />);
    expect(await screen.findByText("understanding")).toBeInTheDocument();
    screen.getByRole("button", { name: "恢复中心" }).click();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      "/api/recovery?project_id=project-1&task_id=run-1",
      expect.any(Object),
    ));
    view.unmount();
  });
});
