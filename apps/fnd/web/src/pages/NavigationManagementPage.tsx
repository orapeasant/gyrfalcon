import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, RefreshCw, Save, Trash2, X } from "lucide-react";
import { fetchJSON } from "../lib/api";

type FunctionRow = {
  id: string; key: string; name: string; kind: string; target: string;
  icon: string | null; params: Record<string, unknown> | null; enabled: boolean; application_id: string;
};
type ApplicationRow = { id: string; code: string; name: string };
type MenuRow = { id: string; name: string; icon: string | null; enabled: boolean; item_count: number; application_id: string };
type MenuItemRow = {
  id: string; menu_id: string; function_id: string | null; ref_menu_id: string | null;
  sort_order: number; access: "read" | "write"; label_override: string | null;
  icon_override: string | null; enabled: boolean; application_id: string;
};
type Catalog = { applications: ApplicationRow[]; functions: FunctionRow[]; menus: MenuRow[]; menu_items: MenuItemRow[] };
type Section = "functions" | "menus" | "menu-items";

const input: React.CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px", background: "var(--bg)", color: "var(--fg)", fontSize: 13 };
const button: React.CSSProperties = { display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, border: "1px solid var(--border)", borderRadius: 6, padding: "7px 10px", background: "var(--card)", color: "var(--fg)", cursor: "pointer", fontSize: 12 };
const primary: React.CSSProperties = { ...button, border: 0, background: "var(--btn-bg)", color: "var(--btn-fg)", fontWeight: 600 };
const panel: React.CSSProperties = { border: "1px solid var(--border)", borderRadius: 8, background: "var(--card)", padding: 16, marginBottom: 14 };
const label: React.CSSProperties = { display: "grid", gap: 5, color: "var(--fg-muted)", fontSize: 11, fontWeight: 600 };
const th: React.CSSProperties = { textAlign: "left", padding: "9px 11px", color: "var(--fg-muted)", background: "var(--bg)", borderBottom: "1px solid var(--border)", fontSize: 10, textTransform: "uppercase", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "9px 11px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" };

const TITLES: Record<Section, { title: string; description: string }> = {
  functions: { title: "Functions", description: "Define page and capability records that menus can grant access to." },
  menus: { title: "Menus", description: "Create and maintain reusable menus and nested menu groups." },
  "menu-items": { title: "Menu Items", description: "Place Functions or child Menus in a parent Menu and set their order and access." },
};

export function FunctionsPage() { return <NavigationManagementPage section="functions" />; }
export function MenusPage() { return <NavigationManagementPage section="menus" />; }
export function MenuItemsPage() { return <NavigationManagementPage section="menu-items" />; }

function NavigationManagementPage({ section }: { section: Section }) {
  const [catalog, setCatalog] = useState<Catalog>({ applications: [], functions: [], menus: [], menu_items: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editingFunction, setEditingFunction] = useState<FunctionRow | null>(null);
  const [creatingFunction, setCreatingFunction] = useState(false);
  const [editingMenu, setEditingMenu] = useState<MenuRow | null>(null);
  const [creatingMenu, setCreatingMenu] = useState(false);
  const [editingItem, setEditingItem] = useState<MenuItemRow | null>(null);
  const [creatingItem, setCreatingItem] = useState(false);
  const [selectedMenu, setSelectedMenu] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const data = await fetchJSON<Catalog>("/api/security/navigation/catalog");
      setCatalog(data);
      setSelectedMenu((current) => data.menus.some((menu) => menu.id === current)
        ? current : data.menus[0]?.id || "");
    } catch (e: any) { setError(e.message || "Unable to load navigation records"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const currentItems = useMemo(() => catalog.menu_items
    .filter((item) => item.menu_id === selectedMenu)
    .sort((a, b) => a.sort_order - b.sort_order || a.id.localeCompare(b.id)),
  [catalog.menu_items, selectedMenu]);

  function saved(message: string) {
    setEditingFunction(null); setCreatingFunction(false);
    setEditingMenu(null); setCreatingMenu(false);
    setEditingItem(null); setCreatingItem(false);
    setNotice(message); void load();
  }
  async function remove(path: string, labelText: string) {
    if (!confirm(`Delete ${labelText}?`)) return;
    setError(""); setNotice("");
    try { await fetchJSON(path, { method: "DELETE" }); saved(`${labelText} deleted.`); }
    catch (e: any) { setError(e.message || `Could not delete ${labelText.toLowerCase()}`); }
  }

  const { title, description } = TITLES[section];
  return <div style={{ padding: 0, maxWidth: 1180, margin: "0 auto", color: "var(--fg)", overflow: "auto", height: "100%" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
      <div><h1 style={{ fontSize: 18, margin: 0 }}>{title}</h1><div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 3 }}>{description}</div></div>
      <button style={{ ...button, marginLeft: "auto" }} onClick={() => void load()}><RefreshCw size={13} /> Refresh</button>
    </div>
    {error && <div role="alert" style={{ ...panel, color: "var(--red)", borderColor: "var(--red)", background: "var(--error-bg)" }}>{error}</div>}
    {notice && <div role="status" style={{ ...panel, color: "var(--green)", background: "var(--success-bg)" }}>{notice}</div>}
    {loading ? <p style={{ color: "var(--fg-muted)" }}>Loading…</p> : section === "functions"
      ? <FunctionsPanel applications={catalog.applications} rows={catalog.functions} editing={editingFunction} setEditing={setEditingFunction} creating={creatingFunction} setCreating={setCreatingFunction} onSaved={() => saved("Function saved.")} onDelete={(row) => void remove(`/api/security/navigation/functions/${row.id}`, `Function “${row.key}”`)} />
      : section === "menus"
        ? <MenusPanel applications={catalog.applications} rows={catalog.menus} editing={editingMenu} setEditing={setEditingMenu} creating={creatingMenu} setCreating={setCreatingMenu} onSaved={() => saved("Menu saved.")} onDelete={(row) => void remove(`/api/security/navigation/menus/${row.id}`, `Menu “${row.name}”`)} />
        : <MenuItemsPanel catalog={catalog} selectedMenu={selectedMenu} setSelectedMenu={setSelectedMenu} rows={currentItems} editing={editingItem} setEditing={setEditingItem} creating={creatingItem} setCreating={setCreatingItem} onSaved={() => saved("Menu item saved.")} onDelete={(row) => void remove(`/api/security/navigation/menu-items/${row.id}`, "Menu item")} />}
  </div>;
}

function FunctionsPanel({ applications, rows, editing, setEditing, creating, setCreating, onSaved, onDelete }: {
  applications: ApplicationRow[];
  rows: FunctionRow[]; editing: FunctionRow | null; setEditing: (row: FunctionRow | null) => void;
  creating: boolean; setCreating: (value: boolean) => void; onSaved: () => void;
  onDelete: (row: FunctionRow) => void;
}) {
  const [key, setKey] = useState(""); const [name, setName] = useState("");
  const [kind, setKind] = useState("page"); const [target, setTarget] = useState("");
  const [icon, setIcon] = useState(""); const [params, setParams] = useState("{}");
  const [applicationId, setApplicationId] = useState("");
  const [enabled, setEnabled] = useState(true); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => {
    setKey(editing?.key || ""); setName(editing?.name || ""); setKind(editing?.kind || "page");
    setTarget(editing?.target || ""); setIcon(editing?.icon || "");
    setApplicationId(editing?.application_id || applications.find(app => app.code === "FND")?.id || applications[0]?.id || "");
    setParams(JSON.stringify(editing?.params || {}, null, 2)); setEnabled(editing?.enabled ?? true); setError("");
  }, [editing, creating]);
  async function save(event: React.FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(params || "{}"); if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(); }
    catch { setError("Parameters must be a JSON object."); setBusy(false); return; }
    const body = { key, name, kind, target, icon: icon || null, params: parsed, enabled, application_id: applicationId };
    try {
      await fetchJSON(editing ? `/api/security/navigation/functions/${editing.id}` : "/api/security/navigation/functions", {
        method: editing ? "PUT" : "POST", body: JSON.stringify(body),
      }); onSaved();
    } catch (e: any) { setError(e.message || "Could not save Function"); }
    finally { setBusy(false); }
  }
  return <>
    <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}><strong>{rows.length} Functions</strong><button style={{ ...primary, marginLeft: "auto" }} onClick={() => { setEditing(null); setCreating(true); }}><Plus size={13} /> Add Function</button></div>
    {(creating || editing) && <form style={panel} onSubmit={save}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 14 }}><strong>{editing ? `Edit ${editing.key}` : "Create Function"}</strong><button type="button" style={{ ...button, marginLeft: "auto", padding: 5 }} onClick={() => { setEditing(null); setCreating(false); }}><X size={14} /></button></div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
        <label style={label}>Key<input style={input} required disabled={!!editing} pattern="[a-zA-Z0-9_.-]+" value={key} onChange={e => setKey(e.target.value)} /></label>
        <label style={label}>Name<input style={input} required value={name} onChange={e => setName(e.target.value)} /></label>
        <label style={label}>Kind<select style={input} value={kind} onChange={e => setKind(e.target.value)}>{["page", "code", "flow", "agent", "skill", "mcp"].map(value => <option key={value}>{value}</option>)}</select></label>
        <label style={label}>Target<input style={input} required value={target} onChange={e => setTarget(e.target.value)} placeholder={kind === "page" ? "registered page key" : "target identifier"} /></label>
        <label style={label}>Application<select style={input} required value={applicationId} onChange={e => setApplicationId(e.target.value)}>{applications.map(app => <option key={app.id} value={app.id}>{app.code} · {app.name}</option>)}</select></label>
        <label style={label}>Icon<input style={input} value={icon} onChange={e => setIcon(e.target.value)} placeholder="Optional Lucide icon name" /></label>
        <label style={{ ...label, display: "flex", alignItems: "center", gridAutoFlow: "column", justifyContent: "start", marginTop: 18 }}><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />Enabled</label>
      </div>
      <label style={{ ...label, marginTop: 10 }}>Parameters (JSON)<textarea style={{ ...input, minHeight: 78, fontFamily: "monospace" }} value={params} onChange={e => setParams(e.target.value)} /></label>
      {error && <p role="alert" style={{ color: "var(--red)", fontSize: 12 }}>{error}</p>}
      <button style={{ ...primary, marginTop: 12 }} disabled={busy}><Save size={13} />{busy ? "Saving…" : "Save Function"}</button>
    </form>}
    <Table headers={["Key", "Name", "Kind", "Target", "Status", ""]}>
      {rows.map(row => <tr key={row.id}><td style={td}><code>{row.key}</code></td><td style={td}>{row.name}</td><td style={td}>{row.kind}</td><td style={td}>{row.target}</td><td style={td}>{row.enabled ? "Enabled" : "Disabled"}</td><td style={td}><Actions onEdit={() => { setCreating(false); setEditing(row); }} onDelete={() => onDelete(row)} /></td></tr>)}
    </Table>
  </>;
}

function MenusPanel({ applications, rows, editing, setEditing, creating, setCreating, onSaved, onDelete }: {
  applications: ApplicationRow[];
  rows: MenuRow[]; editing: MenuRow | null; setEditing: (row: MenuRow | null) => void;
  creating: boolean; setCreating: (value: boolean) => void; onSaved: () => void;
  onDelete: (row: MenuRow) => void;
}) {
  const [name, setName] = useState(""); const [icon, setIcon] = useState("");
  const [applicationId, setApplicationId] = useState("");
  const [enabled, setEnabled] = useState(true); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { setName(editing?.name || ""); setIcon(editing?.icon || ""); setApplicationId(editing?.application_id || applications.find(app => app.code === "FND")?.id || applications[0]?.id || ""); setEnabled(editing?.enabled ?? true); setError(""); }, [editing, creating, applications]);
  async function save(event: React.FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    try { await fetchJSON(editing ? `/api/security/navigation/menus/${editing.id}` : "/api/security/navigation/menus", { method: editing ? "PUT" : "POST", body: JSON.stringify({ name, icon: icon || null, enabled, application_id: applicationId }) }); onSaved(); }
    catch (e: any) { setError(e.message || "Could not save Menu"); } finally { setBusy(false); }
  }
  return <>
    <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}><strong>{rows.length} Menus</strong><button style={{ ...primary, marginLeft: "auto" }} onClick={() => { setEditing(null); setCreating(true); }}><Plus size={13} /> Add Menu</button></div>
    {(creating || editing) && <form style={{ ...panel, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", alignItems: "end", gap: 10 }} onSubmit={save}>
      <label style={label}>Name<input style={input} required value={name} onChange={e => setName(e.target.value)} /></label>
      <label style={label}>Icon<input style={input} value={icon} onChange={e => setIcon(e.target.value)} placeholder="Optional Lucide icon name" /></label>
      <label style={label}>Application<select style={input} required value={applicationId} onChange={e => setApplicationId(e.target.value)}>{applications.map(app => <option key={app.id} value={app.id}>{app.code} · {app.name}</option>)}</select></label>
      <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12 }}><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />Enabled</label>
      <div style={{ display: "flex", gap: 6 }}><button style={primary} disabled={busy}><Save size={13} />Save Menu</button><button type="button" style={button} onClick={() => { setEditing(null); setCreating(false); }}><X size={13} />Cancel</button></div>
      {error && <span role="alert" style={{ color: "var(--red)", fontSize: 12, gridColumn: "1 / -1" }}>{error}</span>}
    </form>}
    <Table headers={["Menu", "Icon", "Menu items", "Status", ""]}>
      {rows.map(row => <tr key={row.id}><td style={td}><strong>{row.name}</strong></td><td style={td}>{row.icon || "—"}</td><td style={td}>{row.item_count}</td><td style={td}>{row.enabled ? "Enabled" : "Disabled"}</td><td style={td}><Actions onEdit={() => { setCreating(false); setEditing(row); }} onDelete={() => onDelete(row)} /></td></tr>)}
    </Table>
  </>;
}

function MenuItemsPanel({ catalog, selectedMenu, setSelectedMenu, rows, editing, setEditing, creating, setCreating, onSaved, onDelete }: {
  catalog: Catalog; selectedMenu: string; setSelectedMenu: (id: string) => void; rows: MenuItemRow[];
  editing: MenuItemRow | null; setEditing: (row: MenuItemRow | null) => void;
  creating: boolean; setCreating: (value: boolean) => void; onSaved: () => void;
  onDelete: (row: MenuItemRow) => void;
}) {
  const [referenceKind, setReferenceKind] = useState<"function" | "menu">("function");
  const [referenceId, setReferenceId] = useState(""); const [sortOrder, setSortOrder] = useState(0);
  const [applicationId, setApplicationId] = useState("");
  const [access, setAccess] = useState<"read" | "write">("read");
  const [labelOverride, setLabelOverride] = useState(""); const [iconOverride, setIconOverride] = useState("");
  const [enabled, setEnabled] = useState(true); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const references = referenceKind === "function" ? catalog.functions : catalog.menus.filter(menu => menu.id !== selectedMenu);
  useEffect(() => {
    const kind = editing?.ref_menu_id ? "menu" : "function";
    const ref = editing?.ref_menu_id || editing?.function_id || "";
    setReferenceKind(kind); setReferenceId(ref || (kind === "function" ? catalog.functions[0]?.id : catalog.menus.find(m => m.id !== selectedMenu)?.id) || "");
    setApplicationId(editing?.application_id || catalog.applications.find(app => app.code === "FND")?.id || catalog.applications[0]?.id || "");
    setSortOrder(editing?.sort_order ?? rows.length); setAccess(editing?.access || "read");
    setLabelOverride(editing?.label_override || ""); setIconOverride(editing?.icon_override || "");
    setEnabled(editing?.enabled ?? true); setError("");
  }, [editing, creating, selectedMenu, rows.length, catalog.applications, catalog.functions, catalog.menus]);
  async function save(event: React.FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    const body = {
      menu_id: editing?.menu_id || selectedMenu,
      application_id: applicationId,
      function_id: !editing && referenceKind === "function" ? referenceId : editing?.function_id || null,
      ref_menu_id: !editing && referenceKind === "menu" ? referenceId : editing?.ref_menu_id || null,
      sort_order: Number(sortOrder), access, label_override: labelOverride || null,
      icon_override: iconOverride || null, enabled,
    };
    try { await fetchJSON(editing ? `/api/security/navigation/menu-items/${editing.id}` : "/api/security/navigation/menu-items", { method: editing ? "PUT" : "POST", body: JSON.stringify(body) }); onSaved(); }
    catch (e: any) { setError(e.message || "Could not save Menu Item"); } finally { setBusy(false); }
  }
  const functionLabel = (id: string | null) => catalog.functions.find(fn => fn.id === id)?.key || "(missing Function)";
  const menuLabel = (id: string | null) => catalog.menus.find(menu => menu.id === id)?.name || "(missing Menu)";
  return <>
    <div style={{ display: "flex", alignItems: "end", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
      <label style={{ ...label, minWidth: 220 }}>Parent Menu<select style={input} value={selectedMenu} onChange={e => { setSelectedMenu(e.target.value); setEditing(null); setCreating(false); }}>{catalog.menus.map(menu => <option key={menu.id} value={menu.id}>{menu.name}</option>)}</select></label>
      <strong style={{ marginBottom: 9 }}>{rows.length} items</strong>
      <button style={{ ...primary, marginLeft: "auto" }} disabled={!selectedMenu} onClick={() => { setEditing(null); setCreating(true); }}><Plus size={13} /> Add Menu Item</button>
    </div>
    {(creating || editing) && <form style={panel} onSubmit={save}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 14 }}><strong>{editing ? "Edit Menu Item" : "Add Menu Item"}</strong><button type="button" style={{ ...button, marginLeft: "auto", padding: 5 }} onClick={() => { setEditing(null); setCreating(false); }}><X size={14} /></button></div>
      {!editing && <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10, marginBottom: 10 }}>
        <label style={label}>Entry type<select style={input} value={referenceKind} onChange={e => { const kind=e.target.value as "function"|"menu"; setReferenceKind(kind); const options=kind === "function" ? catalog.functions : catalog.menus.filter(menu=>menu.id!==selectedMenu); setReferenceId(options[0]?.id || ""); }}><option value="function">Function</option><option value="menu">Child Menu</option></select></label>
        <label style={label}>{referenceKind === "function" ? "Function" : "Child Menu"}<select style={input} required value={referenceId} onChange={e => setReferenceId(e.target.value)}><option value="">Select…</option>{references.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
      </div>}
      {editing && <div style={{ fontSize: 12, color: "var(--fg-muted)", marginBottom: 10 }}>Reference: {editing.function_id ? `Function ${functionLabel(editing.function_id)}` : `Child Menu ${menuLabel(editing.ref_menu_id)}`} (remove and re-add to change the reference)</div>}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", alignItems: "end", gap: 10 }}>
        <label style={label}>Application<select style={input} required value={applicationId} onChange={e => setApplicationId(e.target.value)}>{catalog.applications.map(app => <option key={app.id} value={app.id}>{app.code} · {app.name}</option>)}</select></label>
        <label style={label}>Order<input style={input} type="number" value={sortOrder} onChange={e => setSortOrder(Number(e.target.value))} /></label>
        {referenceKind === "function" && <label style={label}>Access<select style={input} value={access} onChange={e => setAccess(e.target.value as "read"|"write")}><option value="read">Read</option><option value="write">Write</option></select></label>}
        <label style={label}>Label override<input style={input} value={labelOverride} onChange={e => setLabelOverride(e.target.value)} placeholder="Use referenced name" /></label>
        <label style={label}>Icon override<input style={input} value={iconOverride} onChange={e => setIconOverride(e.target.value)} placeholder="Use referenced icon" /></label>
        <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12 }}><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />Enabled</label>
        <div style={{ display: "flex", gap: 6 }}><button style={primary} disabled={busy || (!editing && !referenceId)}><Save size={13} />Save</button><button type="button" style={button} onClick={() => { setEditing(null); setCreating(false); }}>Cancel</button></div>
      </div>
      {error && <p role="alert" style={{ color: "var(--red)", fontSize: 12 }}>{error}</p>}
    </form>}
    <Table headers={["Order", "Type", "Reference", "Label", "Access", "Status", ""]}>
      {rows.map(row => <tr key={row.id}><td style={td}>{row.sort_order}</td><td style={td}>{row.function_id ? "Function" : "Child Menu"}</td><td style={td}>{row.function_id ? functionLabel(row.function_id) : menuLabel(row.ref_menu_id)}</td><td style={td}>{row.label_override || "—"}</td><td style={td}>{row.function_id ? row.access : "—"}</td><td style={td}>{row.enabled ? "Enabled" : "Disabled"}</td><td style={td}><Actions onEdit={() => { setCreating(false); setEditing(row); }} onDelete={() => onDelete(row)} /></td></tr>)}
    </Table>
  </>;
}

function Table({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}><thead><tr>{headers.map(header => <th key={header} style={th}>{header}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}

function Actions({ onEdit, onDelete }: { onEdit: () => void; onDelete: () => void }) {
  return <div style={{ display: "flex", gap: 5 }}><button style={button} onClick={onEdit}>Edit</button><button style={{ ...button, color: "var(--red)" }} onClick={onDelete} title="Delete"><Trash2 size={12} /></button></div>;
}
