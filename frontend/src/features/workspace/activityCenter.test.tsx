import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

import { App } from "../../App";

function json(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("global activity center", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "token" });
      if (url === "/api/projects") {
        return json([{ id: "project-a", name: "项目 A" }, { id: "project-b", name: "项目 B" }]);
      }
      if (url === "/api/activity") {
        return json([{
          id: "run-b",
          kind: "agent.turn",
          status: "completed",
          current_phase: "finalizing",
          attempt: 1,
          unread: true,
          project_id: "project-b",
          project_name: "项目 B",
          payload: { session_id: "chat-b", content: "background" },
        }]);
      }
      if (url.endsWith("/files") || url.endsWith("/agent/tasks")) return json([]);
      if (url.includes("project-a/conversations") && url.includes("/messages?")) return json([]);
      if (url.includes("project-a/conversations")) return json([{ id: "chat-a", title: "A 会话" }]);
      if (url.includes("project-b/conversations") && url.includes("/messages?")) return json([]);
      if (url.includes("project-b/conversations")) return json([{ id: "chat-b", title: "B 会话" }]);
      if (url.endsWith("/runs/run-b/read") && init?.method === "POST") {
        return json({ id: "run-b", status: "completed", unread: false });
      }
      throw new Error(`unexpected request: ${url}`);
    }));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("shows unread completion and navigates without cancelling other runs", async () => {
    render(<App />);
    const activityButton = await screen.findByRole("button", { name: "活动 1" });
    fireEvent.click(activityButton);
    const item = (await screen.findAllByRole("button", { name: /项目 B.*未读/ })).find(
      (button) => button.classList.contains("activity-item"),
    );
    expect(item).toBeDefined();
    if (!item) return;
    fireEvent.click(item);
    await waitFor(() => {
      expect(window.location.pathname).toBe("/projects/project-b/conversations/chat-b");
    });
    const calls = vi.mocked(fetch).mock.calls.map(([input, init]) => ({
      url: String(input),
      method: init?.method ?? "GET",
    }));
    expect(calls).toContainEqual({
      url: "/api/projects/project-b/runs/run-b/read",
      method: "POST",
    });
    expect(calls.filter((call) => call.url.includes("/cancel"))).toHaveLength(0);
  });
});
