/**
 * Markdown — lightweight renderer for agent chat responses.
 * Handles: headings, bold, italic, inline code, fenced code blocks,
 * bullet + numbered lists, blockquotes, horizontal rules, paragraphs.
 * No external dependencies.
 */
import React from "react";

// ── inline rendering ──────────────────────────────────────────────────────────

function renderInline(text: string, key?: string): React.ReactNode {
  // Split on inline patterns: **bold**, *italic*, `code`, [link](url)
  const parts: React.ReactNode[] = [];
  const re = /(\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+?)`|\[([^\]]+?)\]\(([^)]+?)\))/g;
  let last = 0;
  let m: RegExpExecArray | null;

  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));

    if (m[0].startsWith("**"))      parts.push(<strong key={m.index}>{m[2]}</strong>);
    else if (m[0].startsWith("*"))  parts.push(<em key={m.index}>{m[3]}</em>);
    else if (m[0].startsWith("`"))  parts.push(
      <code key={m.index} style={{
        background: "var(--sidebar-active)",
        border: "1px solid var(--border)",
        borderRadius: "4px",
        padding: "1px 5px",
        fontFamily: "Consolas, 'Courier New', monospace",
        fontSize: "0.88em",
        color: "var(--fg)",
      }}>{m[4]}</code>
    );
    else if (m[0].startsWith("["))  parts.push(
      <a key={m.index} href={m[6]} target="_blank" rel="noreferrer"
        style={{ color: "var(--blue)", textDecoration: "underline" }}>
        {m[5]}
      </a>
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length === 1 ? parts[0] : <React.Fragment key={key}>{parts}</React.Fragment>;
}

// ── block parsing ─────────────────────────────────────────────────────────────

type Block =
  | { type: "h1" | "h2" | "h3" | "h4"; text: string }
  | { type: "hr" }
  | { type: "code"; lang: string; text: string }
  | { type: "blockquote"; text: string }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] }
  | { type: "p"; text: string };

function parseBlocks(markdown: string): Block[] {
  const lines = markdown.split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // consume closing ```
      blocks.push({ type: "code", lang, text: codeLines.join("\n") });
      continue;
    }

    // Headings
    const hMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (hMatch) {
      const level = hMatch[1].length;
      const t = `h${Math.min(level, 4)}` as "h1" | "h2" | "h3" | "h4";
      blocks.push({ type: t, text: hMatch[2] });
      i++;
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    // Blockquote
    if (line.startsWith("> ")) {
      const bqLines = [line.slice(2)];
      i++;
      while (i < lines.length && lines[i].startsWith("> ")) {
        bqLines.push(lines[i].slice(2));
        i++;
      }
      blocks.push({ type: "blockquote", text: bqLines.join("\n") });
      continue;
    }

    // Unordered list
    if (/^(\s*[-*+])\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^(\s*[-*+])\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i++;
      }
      blocks.push({ type: "ul", items });
      continue;
    }

    // Ordered list
    if (/^\d+\.\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ type: "ol", items });
      continue;
    }

    // Empty line — skip
    if (line.trim() === "") { i++; continue; }

    // Paragraph — accumulate until blank line or block element
    const pLines = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^(#{1,4}\s|```|> |\d+\. |[-*+] |---|___)/.test(lines[i])
    ) {
      pLines.push(lines[i]);
      i++;
    }
    blocks.push({ type: "p", text: pLines.join(" ") });
  }

  return blocks;
}

// ── styles ────────────────────────────────────────────────────────────────────

const heading: Record<string, React.CSSProperties> = {
  h1: { fontSize: "1.25rem", fontWeight: 700, margin: "0.8rem 0 0.4rem", lineHeight: 1.3 },
  h2: { fontSize: "1.05rem", fontWeight: 700, margin: "0.75rem 0 0.35rem", lineHeight: 1.3 },
  h3: { fontSize: "0.95rem", fontWeight: 700, margin: "0.6rem 0 0.25rem", lineHeight: 1.3 },
  h4: { fontSize: "0.9rem",  fontWeight: 600, margin: "0.5rem 0 0.2rem",  lineHeight: 1.3 },
};

// ── component ─────────────────────────────────────────────────────────────────

interface MarkdownProps {
  content: string;
  style?: React.CSSProperties;
}

function MarkdownImpl({ content, style }: MarkdownProps) {
  // Re-parsed on every render otherwise — during streaming this runs against the
  // whole accumulated message on each repaint.
  const blocks = React.useMemo(() => parseBlocks(content), [content]);

  return (
    <div style={{ lineHeight: 1.65, fontSize: "0.94rem", ...style }}>
      {blocks.map((block, idx) => {
        switch (block.type) {

          case "h1": case "h2": case "h3": case "h4":
            return React.createElement(
              block.type, { key: idx, style: heading[block.type] },
              renderInline(block.text)
            );

          case "hr":
            return <hr key={idx} style={{ border: "none", borderTop: "1px solid var(--border)", margin: "0.75rem 0" }} />;

          case "code":
            return (
              <pre key={idx} style={{
                background: "var(--bg)",
                border: "1px solid var(--border)",
                borderRadius: "7px",
                padding: "0.75rem 1rem",
                overflowX: "auto",
                fontSize: "0.83rem",
                lineHeight: 1.55,
                fontFamily: "Consolas, 'Courier New', monospace",
                margin: "0.6rem 0",
                color: "var(--fg)",
              }}>
                {block.lang && (
                  <div style={{
                    fontSize: "0.72rem", color: "var(--fg-muted)",
                    marginBottom: "0.4rem", fontFamily: "inherit",
                  }}>
                    {block.lang}
                  </div>
                )}
                <code>{block.text}</code>
              </pre>
            );

          case "blockquote":
            return (
              <blockquote key={idx} style={{
                borderLeft: "3px solid var(--fg-muted)",
                margin: "0.5rem 0",
                paddingLeft: "0.85rem",
                color: "var(--fg-muted)",
                fontStyle: "italic",
              }}>
                {renderInline(block.text)}
              </blockquote>
            );

          case "ul":
            return (
              <ul key={idx} style={{ margin: "0.4rem 0", paddingLeft: "1.4rem" }}>
                {block.items.map((item, j) => (
                  <li key={j} style={{ marginBottom: "0.2rem" }}>
                    {renderInline(item)}
                  </li>
                ))}
              </ul>
            );

          case "ol":
            return (
              <ol key={idx} style={{ margin: "0.4rem 0", paddingLeft: "1.4rem" }}>
                {block.items.map((item, j) => (
                  <li key={j} style={{ marginBottom: "0.2rem" }}>
                    {renderInline(item)}
                  </li>
                ))}
              </ol>
            );

          case "p":
            return (
              <p key={idx} style={{ margin: "0.35rem 0" }}>
                {renderInline(block.text)}
              </p>
            );

          default:
            return null;
        }
      })}
    </div>
  );
}

export const Markdown = React.memo(MarkdownImpl);
