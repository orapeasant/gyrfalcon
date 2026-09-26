import React, { useState } from "react";
import { LayoutGrid, List } from "lucide-react";

export type CollectionView = "tiles" | "list";

export function useCollectionView(key: string): [CollectionView, (view: CollectionView) => void] {
  const [view, setView] = useState<CollectionView>(() => {
    try { return localStorage.getItem(`gyrfalcon-view-${key}`) === "list" ? "list" : "tiles"; }
    catch { return "tiles"; }
  });
  const change = (next: CollectionView) => {
    setView(next);
    try { localStorage.setItem(`gyrfalcon-view-${key}`, next); } catch { /* storage can be unavailable */ }
  };
  return [view, change];
}

export function CollectionViewToggle({ view, onChange, label = "Collection view" }: {
  view: CollectionView; onChange: (view: CollectionView) => void; label?: string;
}) {
  const button = (mode: CollectionView, Icon: typeof LayoutGrid, title: string): React.ReactNode => (
    <button key={mode} type="button" aria-label={`${label}: ${title}`} aria-pressed={view === mode}
      onClick={() => onChange(mode)} title={title}
      style={{ display: "flex", alignItems: "center", padding: "5px 7px", border: 0, cursor: "pointer",
        color: view === mode ? "var(--fg)" : "var(--fg-muted)", background: view === mode ? "var(--sidebar-active)" : "transparent" }}>
      <Icon size={14} />
    </button>
  );
  return <div role="group" aria-label={label} style={{ display: "flex", border: "1px solid var(--border)", borderRadius: 6, overflow: "hidden" }}>
    {button("tiles", LayoutGrid, "Square tiles")}{button("list", List, "List")}
  </div>;
}

export function collectionStyle(view: CollectionView): React.CSSProperties {
  return view === "tiles"
    ? { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))", gap: 8, alignItems: "start" }
    : { display: "flex", flexDirection: "column", gap: 0, border: "1px solid var(--border)", borderRadius: 7, overflow: "hidden" };
}

export function collectionCardStyle(view: CollectionView): React.CSSProperties {
  return view === "tiles"
    ? { aspectRatio: "1 / 1", boxSizing: "border-box", overflow: "auto", marginBottom: 0, padding: 8 }
    : { minHeight: 0, boxSizing: "border-box", marginBottom: 0, padding: "8px 12px", borderRadius: 0,
        borderLeft: 0, borderRight: 0, borderTop: 0, display: "flex", flexFlow: "row wrap", alignItems: "center", gap: 12 };
}
