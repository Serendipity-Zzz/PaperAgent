import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import { HistoricalRunTrace } from "./RunTraceCard";
import { runPhaseLabel } from "./runTraceModel";

describe("run trace presentation", () => {
  it("prefers terminal status over a stale current phase", () => {
    expect(runPhaseLabel({ id: "run-1", status: "completed", current_phase: "finalizing" }))
      .toBe("已完成");
    expect(runPhaseLabel({ id: "run-2", status: "failed", current_phase: "queued" }))
      .toBe("执行失败");
  });

  it("renders a terminal trace collapsed inside its owning message", () => {
    render(
      <HistoricalRunTrace
        run={{ id: "run-1", status: "completed", attempt: 1 }}
        elapsed="1分45秒"
        events={[]}
      />,
    );
    const details = screen.getByText("已完成 · 已处理 1分45秒").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("查看公开活动")).toBeInTheDocument();
  });
});
