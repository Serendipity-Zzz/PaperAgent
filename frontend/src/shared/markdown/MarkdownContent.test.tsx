import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import { MarkdownContent } from "./MarkdownContent";

describe("MarkdownContent", () => {
  it("renders GFM content instead of source markdown", () => {
    const { container } = render(
      <MarkdownContent content={"## 结论\n\n- **通过**\n\n| A | B |\n|---|---|\n| 1 | 2 |"} />,
    );
    expect(screen.getByRole("heading", { name: "结论" })).toBeInTheDocument();
    expect(screen.getByText("通过")).toHaveProperty("tagName", "STRONG");
    expect(container.querySelector("table")).toBeInTheDocument();
  });

  it("does not execute raw html or load remote images", () => {
    const { container } = render(
      <MarkdownContent content={'<script>alert(1)</script>\n\n![tracking](https://evil.invalid/pixel.png)'} />,
    );
    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByText("[图片：tracking]")).toBeInTheDocument();
  });

  it("neutralizes unsafe link schemes", () => {
    render(<MarkdownContent content={"[unsafe](javascript:alert(1))"} />);
    expect(screen.getByText("unsafe").closest("a")).not.toHaveAttribute("href");
    expect(screen.queryByRole("link", { name: "unsafe" })).not.toBeInTheDocument();
  });
});
