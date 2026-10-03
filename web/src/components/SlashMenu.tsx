/**
 * SlashMenu — autocomplete dropdown triggered by typing "/" in the chat input.
 *
 * Shows Skills, MCP tools, and Toolsets grouped by category.
 * - Arrow Up/Down to navigate
 * - Tab or Enter to complete (replaces the /query with the completion text)
 * - Escape to dismiss
 */
import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "../lib/api";
import { BookOpen, Server, Wrench, ChevronRight } from "lucide-react";

// ── types ─────────────────────────────────────────────────────────────────────

export type SlashCategory = "skill" | "mcp" | "toolset" | "command";

export interface SlashItem {
  category: SlashCategory;
  label: string;          // display name
  value: string;          // text inserted into input
  description?: string;
  icon?: string;
}

// ── data loading ──────────────────────────────────────────────────────────────

async function loadSlashItems(): Promise<SlashItem[]> {
  const items: SlashItem[] = [];

  // Built-in commands
  items.push(
    { category: "command", label: "allow-all", value: "/allow-all",     description: "Allow all tool calls without approval prompts" },
    { category: "command", label: "allow-all off", value: "/allow-all off", description: "Re-enable approval prompts" },
  );

  try {
    const skillsData = await api.getSkills();
    for (const s of skillsData.skills || []) {
      items.push({
        category: "skill",
        label: s.name,
        value: `/skill ${s.name}`,
        description: s.description || "skill",
      });
    }
  } catch {}

  try {
    const mcpData = await api.getMcpServers();
    for (const srv of mcpData.servers || []) {
      if (!srv.enabled) continue;
      // Add the server itself
      items.push({
        category: "mcp",
        label: srv.name,
        value: `/mcp ${srv.name}`,
        description: `${srv.tools?.length ?? 0} tool${srv.tools?.length === 1 ? "" : "s"}`,
      });
      // Add individual tools
      for (const tool of srv.tools || []) {
        items.push({
          category: "mcp",
          label: `${srv.name}/${tool}`,
          value: `/tool ${tool}`,
          description: `via ${srv.name}`,
        });
      }
    }
  } catch {}

  try {
    const tsData = await api.getToolsets();
    for (const ts of tsData.toolsets || []) {
      const name = typeof ts === "string" ? ts : ts.name ?? String(ts);
      items.push({
        category: "toolset",
        label: name,
        value: `/toolset ${name}`,
        description: "toolset",
      });
    }
  } catch {}

  return items;
}

// ── category meta ─────────────────────────────────────────────────────────────

const CAT_META: Record<SlashCategory, { label: string; Icon: React.ElementType; color: string }> = {
  command: { label: "Commands", Icon: ChevronRight, color: "var(--fg-muted)" },
  skill:   { label: "Skills",   Icon: BookOpen,     color: "var(--blue)" },
  mcp:     { label: "MCP",      Icon: Server,       color: "#22c55e" },
  toolset: { label: "Toolsets", Icon: Wrench,       color: "var(--fg-muted)" },
};

// ── component ─────────────────────────────────────────────────────────────────

interface SlashMenuProps {
  query: string;
  onSelect: (value: string) => void;
  onClose: () => void;
  onKeyDown?: (e: React.KeyboardEvent) => boolean;
  anchorRef: React.RefObject<HTMLElement | null>;
  /** Ref that will receive the keyboard handler — ChatPage calls this from its onKeyDown */
  keyHandlerRef?: React.MutableRefObject<((e: React.KeyboardEvent) => boolean) | null>;
}

export function SlashMenu({ query, onSelect, onClose, anchorRef, keyHandlerRef }: SlashMenuProps) {
  const [allItems, setAllItems] = useState<SlashItem[]>([]);
  const [loading, setLoading]   = useState(true);
  const [cursor, setCursor]     = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  // Load once
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadSlashItems().then(items => {
      if (!cancelled) { setAllItems(items); setLoading(false); }
    });
    return () => { cancelled = true; };
  }, []);

  // Filter by query
  const filtered = query
    ? allItems.filter(item =>
        item.label.toLowerCase().includes(query.toLowerCase()) ||
        item.value.toLowerCase().includes(query.toLowerCase()) ||
        (item.description || "").toLowerCase().includes(query.toLowerCase())
      )
    : allItems;

  // Cap at 12
  const visible = filtered.slice(0, 12);

  // Reset cursor when filter changes
  useEffect(() => { setCursor(0); }, [query]);

  // Scroll cursor into view
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-idx="${cursor}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  function confirm(idx: number) {
    const item = visible[idx];
    if (item) onSelect(item.value);
  }

  // Public keyboard handler — called from ChatPage's onKeyDown
  // Returns true if the event was consumed
  function handleKey(e: React.KeyboardEvent): boolean {
    if (e.key === "Escape") { e.preventDefault(); onClose(); return true; }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor(c => Math.min(c + 1, visible.length - 1));
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor(c => Math.max(c - 1, 0));
      return true;
    }
    if (e.key === "Tab" || (e.key === "Enter" && visible.length > 0)) {
      e.preventDefault();
      confirm(cursor);
      return true;
    }
    return false;
  }

  // Expose handleKey via the keyHandlerRef so ChatPage's onKeyDown can call it
  const handleKeyRef = useRef(handleKey);
  handleKeyRef.current = handleKey;
  useEffect(() => {
    if (keyHandlerRef) keyHandlerRef.current = (e) => handleKeyRef.current(e);
    return () => { if (keyHandlerRef) keyHandlerRef.current = null; };
  }, [keyHandlerRef]);

  if (!loading && visible.length === 0) return null;

  // Group by category for rendering
  const groups: Record<SlashCategory, SlashItem[]> = { command: [], skill: [], mcp: [], toolset: [] };
  let globalIdx = 0;
  const indexedVisible: (SlashItem & { _idx: number })[] = visible.map(item => ({
    ...item, _idx: globalIdx++,
  }));
  for (const item of indexedVisible) groups[item.category].push(item as any);

  return (
    <div
      style={{
        position: "absolute",
        bottom: "calc(100% + 6px)",
        left: 0, right: 0,
        background: "var(--card)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
        boxShadow: "var(--shadow-popover)",
        zIndex: 1000,
        maxHeight: "320px",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Header */}
      <div style={{
        padding: "7px 12px 5px",
        borderBottom: "1px solid var(--border)",
        fontSize: "11px",
        color: "var(--fg-muted)",
        fontWeight: 600,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexShrink: 0,
      }}>
        <span>
          {query ? `/${query}` : "/ — type to filter"}
        </span>
        <span style={{ fontWeight: 400, letterSpacing: 0, textTransform: "none" }}>
          ↑↓ navigate · Tab complete · Esc dismiss
        </span>
      </div>

      {/* Items */}
      <div ref={listRef} style={{ overflowY: "auto", flex: 1 }}>
        {loading && (
          <div style={{ padding: "12px 14px", color: "var(--fg-muted)", fontSize: "13px" }}>
            Loading…
          </div>
        )}

        {(["command", "skill", "mcp", "toolset"] as SlashCategory[]).map(cat => {
          const catItems = groups[cat];
          if (!catItems.length) return null;
          const { label, Icon, color } = CAT_META[cat];
          return (
            <div key={cat}>
              {/* Category header */}
              <div style={{
                padding: "5px 12px 2px",
                fontSize: "10px",
                fontWeight: 700,
                color: "var(--fg-muted)",
                textTransform: "uppercase",
                letterSpacing: "0.07em",
                display: "flex",
                alignItems: "center",
                gap: "5px",
                background: "var(--card)",
                position: "sticky",
                top: 0,
              }}>
                <Icon size={10} color={color} /> {label}
              </div>

              {catItems.map((item: any) => {
                const idx = item._idx as number;
                const active = idx === cursor;
                return (
                  <div
                    key={item.label}
                    data-idx={idx}
                    onClick={() => confirm(idx)}
                    onMouseEnter={() => setCursor(idx)}
                    style={{
                      padding: "7px 14px",
                      cursor: "pointer",
                      background: active ? "var(--sidebar-active)" : "transparent",
                      borderLeft: active ? `2px solid ${color}` : "2px solid transparent",
                      display: "flex",
                      alignItems: "center",
                      gap: "10px",
                      transition: "background 0.08s",
                    }}
                  >
                    <Icon size={13} color={color} style={{ flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{
                        fontSize: "13px",
                        fontWeight: active ? 600 : 400,
                        color: "var(--fg)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}>
                        {item.label}
                      </div>
                      {item.description && (
                        <div style={{
                          fontSize: "11px",
                          color: "var(--fg-muted)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}>
                          {item.description}
                        </div>
                      )}
                    </div>
                    <code style={{
                      fontSize: "10px",
                      color: "var(--fg-muted)",
                      background: "var(--bg)",
                      border: "1px solid var(--border)",
                      borderRadius: "4px",
                      padding: "1px 5px",
                      flexShrink: 0,
                      maxWidth: "140px",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}>
                      {item.value}
                    </code>
                    {active && <ChevronRight size={12} color="var(--fg-muted)" />}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Hook to detect a slash-command query in a textarea value.
 * Returns { active, query } where query is the text after the last "/" at
 * the start of a word at the cursor position.
 */
export function useSlashQuery(value: string, cursorPos: number): { active: boolean; query: string; slashStart: number } {
  // Find the last "/" before cursor that is at start-of-input or after a space/newline
  const textBeforeCursor = value.slice(0, cursorPos);
  const match = textBeforeCursor.match(/(?:^|\s)\/(\S*)$/);
  if (!match) return { active: false, query: "", slashStart: -1 };
  const slashStart = textBeforeCursor.lastIndexOf("/");
  return { active: true, query: match[1], slashStart };
}
