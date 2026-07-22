import { selectPreviewRenderer } from "./registry";

describe("selectPreviewRenderer", () => {
  it.each([
    ["application/pdf", "paper.bin", "pdf"],
    ["image/svg+xml", "standing-wave.svg", "html"],
    ["text/plain", "README.md", "markdown"],
    ["application/octet-stream", "main.py", "code"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "x", "office"],
    ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "x", "table"],
    ["application/octet-stream", "archive.unknown", "metadata"],
  ])("routes %s / %s to %s", (media, file, expected) => {
    expect(selectPreviewRenderer(media, file)).toBe(expected);
  });
});
