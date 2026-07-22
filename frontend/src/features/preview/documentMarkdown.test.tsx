import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import { PreviewPane, type PreviewArtifact, type PreviewPart } from "../../PreviewPane";
import { DEFAULT_PREVIEW_VIEW_STATE } from "./tabStore";

describe("whole-document Markdown preview", () => {
  it("parses all streamed parts as one AST so tables remain intact", () => {
    const artifact: PreviewArtifact = {
      id: "preview-1",
      source_file_id: "file-1",
      source_hash: "a".repeat(64),
      source_name: "report.md",
      media_type: "text/markdown",
      status: "ready",
      fidelity: "structured",
      renderer: "markdown",
      capabilities: [],
      payload: {},
      part_count: 3,
    };
    const parts: PreviewPart[] = [
      { index: 0, kind: "text", label: "heading", payload: { text: "## 结果" }, anchor: null },
      { index: 1, kind: "text", label: "table", payload: { text: "| A | B |\n|---|---|" }, anchor: null },
      { index: 2, kind: "text", label: "row", payload: { text: "| 1 | 2 |" }, anchor: null },
    ];

    const { container } = render(
      <PreviewPane
        projectId="project-1"
        token="token"
        artifact={artifact}
        parts={parts}
        viewState={{ ...DEFAULT_PREVIEW_VIEW_STATE }}
        onViewStateChange={() => undefined}
        onStatus={() => undefined}
        onLoadMore={() => undefined}
      />,
    );

    expect(screen.getByRole("heading", { name: "结果" })).toBeInTheDocument();
    expect(container.querySelectorAll(".markdown-content")).toHaveLength(1);
    expect(container.querySelector("table")).toBeInTheDocument();
  });
});
