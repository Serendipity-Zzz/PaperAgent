import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeAll, beforeEach, vi } from "vitest";

import { App } from "../../App";

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const running = {
  id: "run-a",
  kind: "agent.turn",
  status: "running",
  current_phase: "draft",
  attempt: 1,
  started_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:01Z",
  payload: { session_id: "chat-1", content: "write A" },
};

const envelope = {
  decision_id: "decision-1",
  target_run_id: "run-a",
  response_mode: "sidecar",
  relationship: "independent",
  impact_level: "L0",
  action_on_a: "none",
  affected_nodes: [],
  preserved_nodes: [],
  confidence: 1,
  confirmation_required: false,
  rationale_summary: "B 与 A 独立, A 继续运行。",
  permission_scopes: [],
};

describe("live run steering", () => {
  beforeAll(async () => {
    // These assertions exercise steering state, not the lazy Markdown chunk.
    // Preload that shared chunk so parallel Vitest workers cannot leave message
    // content behind Suspense long enough to make the steering test flaky.
    await import("../../shared/markdown/MarkdownContent");
  });

  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => vi.unstubAllGlobals());

  it("routes B through steering while A keeps running", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "token" });
      if (url === "/api/projects") return json([{ id: "project-1", name: "项目" }]);
      if (url === "/api/activity") return json([]);
      if (url.endsWith("/files")) return json([]);
      if (url.endsWith("/conversations")) return json([{ id: "chat-1", title: "会话" }]);
      if (url.endsWith("/messages") && init?.method === "POST") {
        return json({
          id: "message-b",
          session_id: "chat-1",
          role: "user",
          content: "另外问一个独立问题",
          created_at: "2026-07-18T00:00:02Z",
        }, 201);
      }
      if (url.includes("/messages?")) return json([]);
      if (url.endsWith("/agent/tasks")) return json([running]);
      if (url.endsWith("/agent/tasks/run-a")) return json(running);
      if (url.endsWith("/agent/tasks/run-a/inspect")) {
        return json({ task: running, events: [], approvals: [] });
      }
      if (url.includes("/runs/run-a/events/stream")) return new Response("");
      if (url.endsWith("/runs/run-a/steer") && init?.method === "POST") {
        return json({
          status: "applied",
          envelope,
          target_run: running,
          replacement_run_id: null,
          message: {
            id: "sidecar-answer",
            session_id: "chat-1",
            role: "assistant",
            content: "旁路回答",
            created_at: "2026-07-18T00:00:03Z",
          },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    }));

    const view = render(<App />);
    expect(await screen.findByText("draft")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "描述你的论文或报告需求" }), {
      target: { value: "另外问一个独立问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("旁路回答")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/L0 · independent/)).toBeInTheDocument());
    const calls = vi.mocked(fetch).mock.calls.map(([input, init]) => ({
      url: String(input),
      method: init?.method ?? "GET",
    }));
    expect(calls).toContainEqual({
      url: "/api/projects/project-1/runs/run-a/steer",
      method: "POST",
    });
    expect(calls.some((call) => call.url.includes("/agent/jobs"))).toBe(false);
    view.unmount();
  });

  it("restores and folds superseded output without deleting it", async () => {
    const superseded = { ...running, status: "superseded", finished_at: "2026-07-18T00:00:04Z" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "token" });
      if (url === "/api/projects") return json([{ id: "project-1", name: "项目" }]);
      if (url === "/api/activity") return json([]);
      if (url.endsWith("/files")) return json([]);
      if (url.endsWith("/conversations")) return json([{ id: "chat-1", title: "会话" }]);
      if (url.includes("/messages?")) {
        return json([{
          id: "old-output",
          session_id: "chat-1",
          role: "assistant",
          run_id: "run-a",
          content: "被替代但仍可追溯的正文",
          created_at: "2026-07-18T00:00:03Z",
        }]);
      }
      if (url.endsWith("/agent/tasks")) return json([superseded]);
      if (url.endsWith("/runs/run-a/steering")) {
        return json([{ id: "decision-1", status: "applied", envelope }]);
      }
      throw new Error(`unexpected request: ${url}`);
    }));

    const view = render(<App />);
    const summary = await screen.findByText("已被修订替代的输出");
    expect(screen.getByText("被替代但仍可追溯的正文")).not.toBeVisible();
    fireEvent.click(summary);
    expect(screen.getByText("被替代但仍可追溯的正文")).toBeVisible();
    expect(await screen.findByText(/L0 · independent/)).toBeInTheDocument();
    view.unmount();
  });
});
