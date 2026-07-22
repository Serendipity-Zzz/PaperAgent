export type PreviewRendererKind =
  | "pdf"
  | "image"
  | "markdown"
  | "text"
  | "code"
  | "table"
  | "office"
  | "slides"
  | "html"
  | "metadata";

const CODE_SUFFIXES = new Set([
  "py", "ts", "tsx", "js", "jsx", "java", "c", "cpp", "h", "hpp", "rs", "go", "sql", "sh", "ps1", "yaml", "yml", "toml", "xml",
]);

export function selectPreviewRenderer(mediaType: string, fileName: string): PreviewRendererKind {
  const media = mediaType.toLocaleLowerCase().split(";", 1)[0].trim();
  const suffix = fileName.toLocaleLowerCase().split(".").pop() ?? "";
  if (media === "application/pdf" || suffix === "pdf") return "pdf";
  if (media === "image/svg+xml" || suffix === "svg") return "html";
  if (media.startsWith("image/") && media !== "image/svg+xml") return "image";
  if (media === "text/markdown" || suffix === "md" || suffix === "markdown") return "markdown";
  if (media === "text/html" || suffix === "html" || suffix === "htm") return "html";
  if (media.includes("spreadsheet") || media === "text/csv" || ["csv", "xlsx", "xls", "tsv"].includes(suffix)) return "table";
  if (media.includes("presentation") || ["pptx", "ppt", "odp"].includes(suffix)) return "slides";
  if (media.includes("wordprocessing") || ["docx", "doc", "odt", "rtf"].includes(suffix)) return "office";
  if (CODE_SUFFIXES.has(suffix)) return "code";
  if (media.startsWith("text/") || ["txt", "json", "log"].includes(suffix)) return "text";
  return "metadata";
}
