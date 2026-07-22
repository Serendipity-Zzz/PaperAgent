import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { beforeEach, vi } from "vitest";

import { App } from "../../App";

const projects = [
  { id: "project-a", name: "项目 A" },
  { id: "project-b", name: "项目 B" },
];

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("workspace navigation", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/bootstrap-token") return json({ token: "local-token" });
      if (url === "/api/projects") return json(projects);
      if (url.endsWith("/files") || url.endsWith("/agent/tasks")) return json([]);
      if (url.includes("/project-a/conversations")) {
        if (url.endsWith("/messages") && init?.method === "POST") return json({ id: "a2", session_id: "chat-a", role: "user", content: "new message", created_at: "2026-01-02" }, 201);
        if (url.includes("/messages?")) return json([{ id: "a1", session_id: "chat-a", role: "assistant", content: "A history", created_at: "2026-01-01" }]);
        return json([{ id: "chat-a", title: "A 会话" }]);
      }
      if (url.includes("/project-b/conversations")) {
        if (url.includes("/messages?")) return json([{ id: "b1", session_id: "chat-b", role: "assistant", content: "B history", created_at: "2026-01-01" }]);
        return json([{ id: "chat-b", title: "B 会话" }]);
      }
      if (url.endsWith("/events")) return new Response("", { status: 200 });
      if (url.endsWith("/sessions/chat-a/agent/jobs")) return json({ id: "task-a", kind: "agent.turn", status: "pending", payload: { session_id: "chat-a", content: "new message" } }, 202);
      throw new Error(`unexpected request: ${url}`);
    }));
  });

  it("switches projects and conversations without creating a new project", async () => {
    render(<App />);
    expect(
      await screen.findByText("A history", undefined, { timeout: 5_000 }),
    ).toBeInTheDocument();
    const projectB = screen.getByRole("button", { name: "项目 B" });
    fireEvent.click(projectB);
    expect(
      await screen.findByText("B history", undefined, { timeout: 5_000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("A history")).not.toBeInTheDocument();
    const calls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
    expect(calls.filter((url) => url === "/api/projects")).toHaveLength(1);
    await waitFor(() => expect(screen.getByRole("button", { name: "B 会话" })).toBeInTheDocument());
  });

  it("sends into the selected conversation without posting a project", async () => {
    render(<App />);
    expect(
      await screen.findByText("A history", undefined, { timeout: 5_000 }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "描述你的论文或报告需求" }), {
      target: { value: "new message" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("new message")).toBeInTheDocument();
    const calls = vi.mocked(fetch).mock.calls.map(([input, init]) => ({
      url: String(input),
      method: init?.method ?? "GET",
    }));
    expect(calls).toContainEqual({
      url: "/api/projects/project-a/conversations/chat-a/messages",
      method: "POST",
    });
    expect(calls).not.toContainEqual({ url: "/api/projects", method: "POST" });
  });
});
