import type { ComponentPropsWithoutRef, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";

type Props = {
  content: string;
  className?: string;
  renderLocalImage?: (src: string, alt: string) => ReactNode;
};

function SafeLink(properties: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const { href, children, node, ...props } = properties;
  void node;
  const safeHref = href && /^(https?:|mailto:|#|\/)/i.test(href) ? href : undefined;
  return (
    <a {...props} href={safeHref} target={safeHref?.startsWith("http") ? "_blank" : undefined} rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function MarkdownContent({ content, className = "", renderLocalImage }: Props) {
  return (
    <div className={`markdown-content ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeSanitize, rehypeKatex, rehypeHighlight]}
        components={{
          a: SafeLink,
          img: ({ alt, src }) => {
            const label = alt || "无描述";
            if (src && !/^(?:https?:|data:|\/)/i.test(src) && renderLocalImage) {
              return renderLocalImage(src, label);
            }
            return <span className="blocked-remote-image">[图片：{label}]</span>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
