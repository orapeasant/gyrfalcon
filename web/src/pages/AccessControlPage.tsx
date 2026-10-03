import React, { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, Save, Trash2, UsersRound, ShieldCheck, X, KeyRound } from "lucide-react";
import { fetchJSON } from "../lib/api";

type UserRow = { id: string; username?: string; display_name: string; email: string; disabled: boolean; kind: string; roles: string[]; group_ids: string[]; last_login_at?: number | null };
type GroupRow = { id: string; name: string; description: string; user_ids: string[] };
type RoleRow = { id: string; name: string; label: string; menu_id: string | null; description: string; enabled: boolean };
type AccessData = { users: UserRow[]; groups: GroupRow[]; roles: RoleRow[]; menus: { id: string; name: string }[] };

const input: React.CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px", background: "var(--bg)", color: "var(--fg)", fontSize: 13 };
const button: React.CSSProperties = { display: "inline-flex", alignItems: "center", gap: 6, border: "1px solid var(--border)", borderRadius: 6, padding: "7px 10px", background: "var(--card)", color: "var(--fg)", cursor: "pointer", fontSize: 12 };
const primary: React.CSSProperties = { ...button, border: 0, background: "var(--btn-bg)", color: "var(--btn-fg)", fontWeight: 600 };
const panel: React.CSSProperties = { border: "1px solid var(--border)", borderRadius: 8, background: "var(--card)", padding: 16, marginBottom: 16 };
const label: React.CSSProperties = { display: "grid", gap: 5, color: "var(--fg-muted)", fontSize: 11, fontWeight: 600 };

export function AccessControlPage({ initialTab = "users" }: { initialTab?: "users" | "roles" }) {
  const [tab, setTab] = useState<"users" | "groups" | "roles">(initialTab);
  const [data, setData] = useState<AccessData>({ users: [], groups: [], roles: [], menus: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editingUser, setEditingUser] = useState<UserRow | null>(null);
  const [editingRole, setEditingRole] = useState<RoleRow | null>(null);
  const [newUser, setNewUser] = useState(false);
  const [groupDraft, setGroupDraft] = useState<GroupRow | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try { setData(await fetchJSON<AccessData>("/api/security/access")); }
    catch (e: any) { setError(e.message || "Unable to load access control data"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setTab(initialTab); }, [initialTab]);

  const tabs = [
    ["users", "Users", UsersRound], ["groups", "Groups", UsersRound], ["roles", "Roles", ShieldCheck],
  ] as const;
  return <div style={{ padding: 0, maxWidth: 1120, margin: "0 auto", color: "var(--fg)" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
      <div><h1 style={{ fontSize: 18, margin: 0 }}>Access Control</h1><div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 3 }}>Manage organization users, groups, and role access.</div></div>
      <button style={{ ...button, marginLeft: "auto" }} onClick={() => void load()}><RefreshCw size={13} /> Refresh</button>
    </div>
    <div role="tablist" style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border)", marginBottom: 16 }}>
      {tabs.map(([key, title, Icon]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => { setTab(key); setError(""); setNotice(""); }}
        style={{ ...button, border: 0, borderBottom: tab === key ? "2px solid var(--primary)" : "2px solid transparent", borderRadius: 0, background: "transparent", color: tab === key ? "var(--fg)" : "var(--fg-muted)" }}><Icon size={14} />{title}</button>)}
    </div>
    {error && <div role="alert" style={{ ...panel, color: "var(--red)", borderColor: "var(--red)", background: "var(--error-bg)", marginBottom: 12 }}>{error}</div>}
    {notice && <div role="status" style={{ ...panel, color: "var(--green)", background: "var(--success-bg)", marginBottom: 12 }}>{notice}</div>}
    {loading ? <p style={{ color: "var(--fg-muted)" }}>Loading…</p> : tab === "users" ? <UsersPanel data={data} editing={editingUser} setEditing={setEditingUser} creating={newUser} setCreating={setNewUser} onSaved={async () => { setEditingUser(null); setNewUser(false); setNotice("User saved."); await load(); }} />
      : tab === "groups" ? <GroupsPanel groups={data.groups} users={data.users} draft={groupDraft} setDraft={setGroupDraft} onSaved={async () => { setGroupDraft(null); setNotice("Group saved."); await load(); }} />
        : <RolesPanel roles={data.roles} menus={data.menus} editing={editingRole} setEditing={setEditingRole} onSaved={async () => { setEditingRole(null); setNotice("Role saved."); await load(); }} />}
  </div>;
}

function UsersPanel({ data, editing, setEditing, creating, setCreating, onSaved }: {
  data: AccessData; editing: UserRow | null; setEditing: (u: UserRow | null) => void;
  creating: boolean; setCreating: (v: boolean) => void; onSaved: () => void;
}) {
  const [displayName, setDisplayName] = useState(""); const [email, setEmail] = useState("");
  const [username, setUsername] = useState(""); const [password, setPassword] = useState("");
  const [roles, setRoles] = useState<string[]>(["app_developer"]); const [groupIds, setGroupIds] = useState<string[]>([]);
  const [disabled, setDisabled] = useState(false); const [resetPassword, setResetPassword] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => {
    setError(""); setDisplayName(editing?.display_name || ""); setEmail(editing?.email || "");
    setUsername(""); setPassword(""); setRoles(editing?.roles || ["app_developer"]);
    setGroupIds(editing?.group_ids || []); setDisabled(editing?.disabled || false); setResetPassword("");
  }, [editing, creating]);
  function toggle(values: string[], value: string, set: (v: string[]) => void) { set(values.includes(value) ? values.filter(x => x !== value) : [...values, value]); }
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try {
      if (creating) await fetchJSON("/api/security/access/users", { method: "POST", body: JSON.stringify({ username, password, display_name: displayName, email, roles, group_ids: groupIds }) });
      else if (editing) {
        await fetchJSON(`/api/security/access/users/${editing.id}`, { method: "PUT", body: JSON.stringify({ display_name: displayName, email, roles, group_ids: groupIds, disabled }) });
        if (resetPassword) await fetchJSON(`/api/security/access/users/${editing.id}/password`, { method: "POST", body: JSON.stringify({ password: resetPassword }) });
      }
      onSaved();
    } catch (err: any) { setError(err.message || "Could not save user"); }
    finally { setBusy(false); }
  }
  const openCreate = () => { setEditing(null); setCreating(true); };
  return <>
    <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}><strong>{data.users.length} users</strong><button style={{ ...primary, marginLeft: "auto" }} onClick={openCreate}><Plus size={13} /> Add user</button></div>
    {(creating || editing) && <form style={panel} onSubmit={save}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 14 }}><strong>{creating ? "Create user" : `Edit ${editing?.username || editing?.display_name}`}</strong><button type="button" style={{ ...button, marginLeft: "auto", padding: 5 }} onClick={() => { setCreating(false); setEditing(null); }}><X size={14} /></button></div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
        {creating && <label style={label}>Username<input style={input} required autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} /></label>}
        <label style={label}>Display name<input style={input} value={displayName} onChange={e => setDisplayName(e.target.value)} /></label>
        <label style={label}>Email<input style={input} type="email" value={email} onChange={e => setEmail(e.target.value)} /></label>
        {creating && <label style={label}>Initial password<input style={input} type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} /></label>}
        {editing && editing.username && <label style={label}>Reset password<input style={input} type="password" autoComplete="new-password" placeholder="Leave blank to keep current password" value={resetPassword} onChange={e => setResetPassword(e.target.value)} /></label>}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16, marginTop: 14 }}>
        <fieldset style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}><legend style={{ color: "var(--fg-muted)", fontSize: 11 }}>Roles</legend>
          {data.roles.map(r => <label key={r.id} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, padding: "3px 0" }}><input type="checkbox" checked={roles.includes(r.name)} onChange={() => toggle(roles, r.name, setRoles)} />{r.label || r.name}</label>)}
          {editing?.roles.includes("admin") && <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, padding: "3px 0", color: "var(--fg-muted)" }}><input type="checkbox" checked disabled />Superuser (protected)</label>}
        </fieldset>
        <fieldset style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 10 }}><legend style={{ color: "var(--fg-muted)", fontSize: 11 }}>Groups</legend>
          {data.groups.length ? data.groups.map(g => <label key={g.id} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, padding: "3px 0" }}><input type="checkbox" checked={groupIds.includes(g.id)} onChange={() => toggle(groupIds, g.id, setGroupIds)} />{g.name}</label>) : <span style={{ fontSize: 12, color: "var(--fg-muted)" }}>Create groups in the Groups tab.</span>}
        </fieldset>
      </div>
      {!creating && <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, marginTop: 12 }}><input type="checkbox" checked={disabled} onChange={e => setDisabled(e.target.checked)} />Account disabled</label>}
      {error && <p style={{ color: "var(--red)", fontSize: 12 }}>{error}</p>}
      <button style={{ ...primary, marginTop: 14 }} disabled={busy}><Save size={13} />{busy ? "Saving…" : "Save user"}</button>
    </form>}
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead><tr>{["User", "Email", "Roles", "Groups", "Last login", "Status", ""].map(h => <th key={h} style={th}>{h}</th>)}</tr></thead>
      <tbody>{data.users.map(u => <tr key={u.id}>
        <td style={td}><strong>{u.display_name || u.username || u.id}</strong><div style={{ color: "var(--fg-muted)", fontSize: 11 }}>{u.username || (u.kind === "service" ? "Service account" : "SSO account")}</div></td>
        <td style={td}>{u.email || "—"}</td><td style={td}>{u.roles.map(r => <Pill key={r}>{r}</Pill>)}</td>
        <td style={td}>{u.group_ids.map(id => data.groups.find(g => g.id === id)?.name).filter(Boolean).map(g => <Pill key={g}>{g}</Pill>) || "—"}</td>
        <td style={td}>{u.last_login_at ? new Date(u.last_login_at * 1000).toLocaleString() : "Never"}</td>
        <td style={td}><span style={{ color: u.disabled ? "var(--red)" : "var(--green)" }}>{u.disabled ? "Disabled" : "Active"}</span></td>
        <td style={td}><button style={button} onClick={() => { setCreating(false); setEditing(u); }}><KeyRound size={12} /> Manage</button></td>
      </tr>)}</tbody>
    </table></div>
  </>;
}

function GroupsPanel({ groups, users, draft, setDraft, onSaved }: { groups: GroupRow[]; users: UserRow[]; draft: GroupRow | null; setDraft: (g: GroupRow | null) => void; onSaved: () => void }) {
  const [name, setName] = useState(""); const [description, setDescription] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { setName(draft?.name || ""); setDescription(draft?.description || ""); setError(""); }, [draft]);
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try { await fetchJSON(draft?.id ? `/api/security/access/groups/${draft.id}` : "/api/security/access/groups", { method: draft?.id ? "PUT" : "POST", body: JSON.stringify({ name, description }) }); onSaved(); }
    catch (err: any) { setError(err.message || "Could not save group"); } finally { setBusy(false); }
  }
  async function remove(group: GroupRow) {
    if (!confirm(`Delete group “${group.name}”? Its user assignments will be removed.`)) return;
    try { await fetchJSON(`/api/security/access/groups/${group.id}`, { method: "DELETE" }); onSaved(); } catch (e: any) { setError(e.message); }
  }
  return <>
    <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}><strong>{groups.length} groups</strong><button style={{ ...primary, marginLeft: "auto" }} onClick={() => setDraft({ id: "", name: "", description: "", user_ids: [] })}><Plus size={13} /> Add group</button></div>
    {draft && <form style={{ ...panel, display: "grid", gridTemplateColumns: "minmax(180px, 1fr) 2fr auto auto", alignItems: "end", gap: 10 }} onSubmit={save}>
      <label style={label}>Group name<input style={input} required value={name} onChange={e => setName(e.target.value)} /></label>
      <label style={label}>Description<input style={input} value={description} onChange={e => setDescription(e.target.value)} /></label>
      <button style={primary} disabled={busy}><Save size={13} />Save</button><button type="button" style={button} onClick={() => setDraft(null)}>Cancel</button>
      {error && <span style={{ gridColumn: "1 / -1", color: "var(--red)", fontSize: 12 }}>{error}</span>}
    </form>}
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead><tr>{["Group", "Description", "Members", ""].map(h => <th key={h} style={th}>{h}</th>)}</tr></thead>
      <tbody>{groups.map(g => <tr key={g.id}><td style={td}><strong><UsersRound size={13} style={{ verticalAlign: "-2px", marginRight: 6 }} />{g.name}</strong></td><td style={td}>{g.description || "—"}</td>
        <td style={td}>{g.user_ids.map(id => users.find(u => u.id === id)?.display_name || users.find(u => u.id === id)?.username).filter(Boolean).join(", ") || "—"}</td>
        <td style={td}><div style={{ display: "flex", gap: 5 }}><button style={button} onClick={() => setDraft(g)}>Edit</button><button style={{ ...button, color: "var(--red)" }} onClick={() => void remove(g)}><Trash2 size={12} /></button></div></td></tr>)}</tbody>
    </table></div>
  </>;
}

function RolesPanel({ roles, menus, editing, setEditing, onSaved }: { roles: RoleRow[]; menus: AccessData["menus"]; editing: RoleRow | null; setEditing: (r: RoleRow | null) => void; onSaved: () => void }) {
  const [name, setName] = useState(""); const [labelText, setLabelText] = useState(""); const [menuId, setMenuId] = useState(""); const [description, setDescription] = useState(""); const [enabled, setEnabled] = useState(true); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { setName(editing?.name || ""); setLabelText(editing?.label || ""); setMenuId(editing?.menu_id || ""); setDescription(editing?.description || ""); setEnabled(editing?.enabled ?? true); setError(""); }, [editing]);
  async function save(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try { await fetchJSON(editing?.id ? `/api/security/access/roles/${editing.id}` : "/api/security/access/roles", { method: editing?.id ? "PUT" : "POST", body: JSON.stringify({ name, label: labelText, menu_id: menuId || null, description, enabled }) }); onSaved(); }
    catch (err: any) { setError(err.message || "Could not save role"); } finally { setBusy(false); }
  }
  async function remove(role: RoleRow) {
    if (!confirm(`Delete role “${role.label || role.name}”? User assignments using it will no longer grant this menu.`)) return;
    try { await fetchJSON(`/api/security/access/roles/${role.id}`, { method: "DELETE" }); onSaved(); } catch (e: any) { setError(e.message); }
  }
  return <>
    <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}><strong>{roles.length} roles</strong><button style={{ ...primary, marginLeft: "auto" }} onClick={() => setEditing({ id: "", name: "", label: "", menu_id: null, description: "", enabled: true })}><Plus size={13} /> Add role</button></div>
    {editing && <form style={{ ...panel, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }} onSubmit={save}>
      <label style={label}>Role key<input style={input} required pattern="[a-zA-Z0-9_.-]+" disabled={!!editing.id} value={name} onChange={e => setName(e.target.value)} /></label>
      <label style={label}>Display name<input style={input} value={labelText} onChange={e => setLabelText(e.target.value)} /></label>
      <label style={label}>Menu<select style={input} value={menuId} onChange={e => setMenuId(e.target.value)}><option value="">No menu</option>{menus.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
      <label style={label}>Description<input style={input} value={description} onChange={e => setDescription(e.target.value)} /></label>
      <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12 }}><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />Enabled</label>
      {error && <div style={{ color: "var(--red)", fontSize: 12, gridColumn: "1 / -1" }}>{error}</div>}
      <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8 }}><button style={primary} disabled={busy}><Save size={13} />Save role</button><button type="button" style={button} onClick={() => setEditing(null)}>Cancel</button></div>
    </form>}
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflowX: "auto" }}><table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead><tr>{["Role", "Menu", "Description", "Status", ""].map(h => <th key={h} style={th}>{h}</th>)}</tr></thead>
      <tbody>{roles.map(r => <tr key={r.id}><td style={td}><strong>{r.label || r.name}</strong><div style={{ color: "var(--fg-muted)", fontSize: 11 }}>{r.name}</div></td><td style={td}>{menus.find(m => m.id === r.menu_id)?.name || "—"}</td><td style={td}>{r.description || "—"}</td><td style={td}>{r.enabled ? "Enabled" : "Disabled"}</td><td style={td}><div style={{ display: "flex", gap: 5 }}><button style={button} onClick={() => setEditing(r)}>Edit</button><button style={{ ...button, color: "var(--red)" }} onClick={() => void remove(r)}><Trash2 size={12} /></button></div></td></tr>)}</tbody>
    </table></div>
  </>;
}

function Pill({ children }: { children: React.ReactNode }) { return <span style={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 10, padding: "2px 7px", fontSize: 10, color: "var(--fg-muted)" }}>{children}</span>; }
const th: React.CSSProperties = { textAlign: "left", padding: "9px 11px", color: "var(--fg-muted)", background: "var(--bg)", borderBottom: "1px solid var(--border)", fontSize: 10, textTransform: "uppercase", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "9px 11px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" };
