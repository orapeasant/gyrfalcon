import React, { useState, useEffect, useCallback } from "react";
import { api } from "../lib/api";

// ── Types ─────────────────────────────────────────────────────────────────────

type ServerType = "stdio" | "openapi" | "odata";
type AuthType = "none" | "basic" | "apikey" | "oauth";

interface EntityConfig {
  enabled: boolean;
  short_code: string;
  description: string;
}

interface ODataEntity {
  name: string;
  entity_type: string;
  key_properties: string[];
  properties: string[];
}

interface OpenAPIOperation {
  operation_id: string;
  method: string;
  path: string;
  summary: string;
  tags: string[];
}

/** Multi-config auth — all type credentials are preserved; only the active type has enabled:true */
interface MultiAuth {
  type: AuthType;
  none:   { enabled: boolean };
  basic:  { enabled: boolean; username: string; password: string };
  apikey: { enabled: boolean; api_key: string; api_key_header: string };
  oauth:  { enabled: boolean; client_id: string; client_secret: string; token_url: string; scope: string };
}

function emptyMultiAuth(active: AuthType = "none"): MultiAuth {
  return {
    type: active,
    none:   { enabled: active === "none" },
    basic:  { enabled: active === "basic",  username: "", password: "" },
    apikey: { enabled: active === "apikey", api_key: "", api_key_header: "Authorization" },
    oauth:  { enabled: active === "oauth",  client_id: "", client_secret: "", token_url: "", scope: "" },
  };
}

/** Migrate old flat auth object → MultiAuth (idempotent if already MultiAuth). */
function migrateAuth(raw: any): MultiAuth {
  if (!raw || typeof raw !== "object") return emptyMultiAuth();
  // Already in new format if it has 'none', 'basic', 'apikey', 'oauth' sub-keys
  if (raw.none !== undefined || raw.oauth !== undefined) return raw as MultiAuth;
  // Old flat format: { type, username, password, api_key, api_key_header }
  const active: AuthType = (raw.type || "none") as AuthType;
  const ma = emptyMultiAuth(active);
  ma.basic.username          = raw.username || "";
  ma.basic.password          = raw.password || "";
  ma.apikey.api_key          = raw.api_key || "";
  ma.apikey.api_key_header   = raw.api_key_header || "Authorization";
  return ma;
}

/** Extract the active auth credentials as flat fields for API calls. */
function resolveAuthCreds(auth: MultiAuth): { username: string; password: string; api_key: string; api_key_header: string; oauth_client_id: string; oauth_client_secret: string; oauth_token_url: string; oauth_scope: string } {
  return {
    username:           auth.basic?.username          || "",
    password:           auth.basic?.password          || "",
    api_key:            auth.apikey?.api_key           || "",
    api_key_header:     auth.apikey?.api_key_header    || "Authorization",
    oauth_client_id:    auth.oauth?.client_id          || "",
    oauth_client_secret:auth.oauth?.client_secret      || "",
    oauth_token_url:    auth.oauth?.token_url          || "",
    oauth_scope:        auth.oauth?.scope              || "",
  };
}

/** Change active auth type while preserving all stored values. */
function switchAuthType(prev: MultiAuth, newType: AuthType): MultiAuth {
  return {
    ...prev,
    type:   newType,
    none:   { ...prev.none,   enabled: newType === "none"   },
    basic:  { ...prev.basic,  enabled: newType === "basic"  },
    apikey: { ...prev.apikey, enabled: newType === "apikey" },
    oauth:  { ...prev.oauth,  enabled: newType === "oauth"  },
  };
}

interface McpServer {
  name: string;
  type: ServerType;
  description: string;
  // stdio
  command: string[] | string;
  args: string[];
  env: Record<string, string>;
  // openapi / odata shared
  url: string;
  service: string;
  auth: MultiAuth | any;
  // odata + openapi entity/operation selection
  entities: Record<string, EntityConfig>;
  // odata-specific
  metadata_url: string;
  metadata_content: string;
  odata_version: "V2" | "V4" | "";
  include_sets: string[];
  exclude_sets: string[];
  operations: { query: boolean; get: boolean; count: boolean; create: boolean; update: boolean; delete: boolean };
  name_overrides: Record<string, string>;
  default_page_size: number;
  max_page_size: number;
  enable_csrf: boolean;
  code_mode: boolean;
  // runtime
  enabled: boolean;
  connected: boolean;
  tools: string[];
}

type EditState = McpServer & { _isNew: boolean };

// ── Helpers ───────────────────────────────────────────────────────────────────

function emptyServer(): EditState {
  return {
    name: "", type: "stdio", description: "",
    command: [], args: [], env: {},
    url: "", service: "",
    auth: emptyMultiAuth("none"),
    entities: {},
    metadata_url: "", metadata_content: "", odata_version: "V2",
    include_sets: [], exclude_sets: [],
    operations: { query: true, get: true, count: true, create: true, update: true, delete: false },
    name_overrides: {}, default_page_size: 0, max_page_size: 0,
    enable_csrf: true, code_mode: false,
    enabled: true, connected: false, tools: [], _isNew: true,
  };
}
const cmdStr = (c: string[] | string) => Array.isArray(c) ? c.join(" ") : c;
const cmdArr = (s: string) => s.trim() ? s.trim().split(/\s+/) : [];

// ── Styles ────────────────────────────────────────────────────────────────────

const cx = {
  layout:        { display:"flex", height:"100%", overflow:"hidden" } as React.CSSProperties,
  sidebar:       { width:"250px", flexShrink:0, borderRight:"1px solid var(--color-border)", display:"flex", flexDirection:"column" as const, background:"var(--color-midground)", overflow:"hidden" },
  sbHeader:      { padding:"0.85rem 1rem", borderBottom:"1px solid var(--color-border)", display:"flex", alignItems:"center", justifyContent:"space-between" },
  sbTitle:       { fontSize:"0.72rem", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.08em", color:"var(--color-muted)", margin:0 },
  addBtn:        { background:"var(--btn-bg)", color:"var(--btn-fg)", border:"none", borderRadius:"5px", padding:"0.22rem 0.6rem", fontSize:"0.8rem", fontWeight:700, cursor:"pointer" },
  list:          { flex:1, overflowY:"auto" as const },
  item:          (active:boolean):React.CSSProperties => ({ padding:"0.6rem 1rem", cursor:"pointer", borderLeft: active ? "3px solid var(--fg-muted)" : "3px solid transparent", background: active ? "var(--sidebar-active)" : "transparent", borderBottom:"1px solid var(--color-border)" }),
  itemName:      (active:boolean):React.CSSProperties => ({ fontWeight: active ? 700 : 400, color: "var(--color-foreground)", fontSize:"0.9rem", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }),
  dot:           (on:boolean):React.CSSProperties => ({ display:"inline-block", width:"7px", height:"7px", borderRadius:"50%", background: on ? "#34d399" : "#6b7280", marginRight:"0.4rem" }),
  main:          { flex:1, overflow:"auto", padding:"1.5rem" } as React.CSSProperties,
  empty:         { display:"flex", flexDirection:"column" as const, alignItems:"center", justifyContent:"center", height:"60vh", color:"var(--color-muted)", gap:"0.5rem" },
  card:          { background:"var(--color-midground)", border:"1px solid var(--color-border)", borderRadius:"8px", padding:"1.5rem", maxWidth:"820px" } as React.CSSProperties,
  cardH:         { display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:"1.25rem", flexWrap:"wrap" as const, gap:"0.75rem" },
  h2:            { fontSize:"1.05rem", fontWeight:700, margin:0, display:"flex", alignItems:"center", gap:"0.5rem" },
  typeSel:       { display:"flex", gap:"0.5rem", marginBottom:"1.25rem" },
  typeBtn:       (active:boolean):React.CSSProperties => ({ padding:"0.35rem 0.9rem", borderRadius:"20px", border: active ? "none" : "1px solid var(--color-border)", background: active ? "var(--btn-bg)" : "transparent", color: active ? "var(--btn-fg)" : "var(--color-muted)", fontWeight: active ? 700 : 400, cursor:"pointer", fontSize:"0.82rem" }),
  field:         { marginBottom:"1rem" } as React.CSSProperties,
  label:         { display:"block", fontSize:"0.72rem", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.07em", color:"var(--color-muted)", marginBottom:"0.3rem" },
  input:         { width:"100%", background:"var(--color-background)", border:"1px solid var(--color-border)", borderRadius:"6px", padding:"0.42rem 0.7rem", color:"var(--color-foreground)", fontSize:"0.875rem", fontFamily:"monospace" } as React.CSSProperties,
  hint:          { fontSize:"0.7rem", color:"var(--color-muted)", marginTop:"0.2rem" },
  row2:          { display:"grid", gridTemplateColumns:"1fr 1fr", gap:"0.75rem" } as React.CSSProperties,
  row3:          { display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap:"0.75rem" } as React.CSSProperties,
  divider:       { border:"none", borderTop:"1px solid var(--color-border)", margin:"1.25rem 0" } as React.CSSProperties,
  sectionH:      { fontSize:"0.78rem", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.07em", color:"var(--color-muted)", marginBottom:"0.75rem" },
  discoverBtn:   (loading:boolean):React.CSSProperties => ({ padding:"0.42rem 1rem", background: loading ? "var(--btn-bg-disabled)" : "var(--btn-bg)", color: loading ? "var(--btn-fg-disabled)" : "var(--btn-fg)", border:"none", borderRadius:"6px", fontWeight:700, fontSize:"0.82rem", cursor: loading ? "not-allowed" : "pointer" }),
  envInput:      { width:"100%", background:"var(--color-background)", border:"1px solid transparent", borderRadius:"4px", padding:"0.28rem 0.45rem", color:"var(--color-foreground)", fontSize:"0.8rem", fontFamily:"monospace" } as React.CSSProperties,
  addEnvBtn:     { background:"transparent", color:"var(--fg-muted)", border:"1px solid var(--border)", borderRadius:"5px", padding:"0.18rem 0.55rem", fontSize:"0.75rem", cursor:"pointer" },
  rmBtn:         { background:"transparent", color:"#f87171", border:"none", cursor:"pointer", fontSize:"0.82rem", padding:"0 0.3rem" },
  actions:       { display:"flex", gap:"0.75rem", marginTop:"1.5rem", flexWrap:"wrap" as const, alignItems:"center" },
  saveBtn:       (dis:boolean):React.CSSProperties => ({ background: dis ? "var(--btn-bg-disabled)" : "var(--btn-bg)", color: dis ? "var(--btn-fg-disabled)" : "var(--btn-fg)", border:"none", borderRadius:"6px", padding:"0.5rem 1.25rem", fontWeight:700, fontSize:"0.875rem", cursor: dis ? "not-allowed" : "pointer" }),
  delBtn:        { background:"transparent", color:"#f87171", border:"1px solid #f87171", borderRadius:"6px", padding:"0.5rem 1rem", fontSize:"0.875rem", cursor:"pointer", marginLeft:"auto" },
  toast:         (t:"success"|"error"):React.CSSProperties => ({ position:"fixed", bottom:"1.5rem", right:"1.5rem", background: t==="success" ? "#064e3b" : "#450a0a", border:`1px solid ${t==="success" ? "#34d399" : "#f87171"}`, borderRadius:"8px", padding:"0.75rem 1.25rem", color: t==="success" ? "#34d399" : "#f87171", fontWeight:600, fontSize:"0.875rem", zIndex:9999, boxShadow:"0 4px 24px rgba(0,0,0,0.5)" }),
  // Entity table
  etWrap:        { border:"1px solid var(--color-border)", borderRadius:"6px", overflow:"hidden" } as React.CSSProperties,
  etHdr:         { display:"flex", alignItems:"center", justifyContent:"space-between", padding:"0.5rem 0.75rem", background:"var(--color-background)", borderBottom:"1px solid var(--color-border)" },
  etHdrLabel:    { fontSize:"0.72rem", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.07em", color:"var(--color-muted)" },
  etSelBtns:     { display:"flex", gap:"0.5rem" },
  etSelBtn:      { background:"transparent", border:"1px solid var(--color-border)", borderRadius:"4px", padding:"0.15rem 0.5rem", fontSize:"0.72rem", color:"var(--color-muted)", cursor:"pointer" },
  etTable:       { width:"100%", borderCollapse:"collapse" as const },
  etTh:          { padding:"0.45rem 0.6rem", fontSize:"0.7rem", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.06em", color:"var(--color-muted)", textAlign:"left" as const, borderBottom:"1px solid var(--color-border)", background:"var(--color-background)" },
  etTd:          { padding:"0.35rem 0.5rem", borderBottom:"1px solid rgba(42,42,58,0.6)", verticalAlign:"middle" as const },
  etName:        { fontFamily:"monospace", fontSize:"0.82rem", color:"var(--color-foreground)" },
  etInput:       { width:"100%", background:"transparent", border:"1px solid transparent", borderRadius:"4px", padding:"0.2rem 0.4rem", color:"var(--color-foreground)", fontSize:"0.8rem", fontFamily:"monospace" } as React.CSSProperties,
  etScInput:     { width:"90px", background:"transparent", border:"1px solid transparent", borderRadius:"4px", padding:"0.2rem 0.4rem", color:"var(--color-foreground)", fontSize:"0.8rem", fontFamily:"monospace" } as React.CSSProperties,
  badge:         (ok:boolean):React.CSSProperties => ({ fontSize:"0.68rem", padding:"0.1rem 0.45rem", borderRadius:"10px", fontWeight:600, background: ok ? "rgba(52,211,153,0.15)" : "rgba(107,114,128,0.2)", color: ok ? "#34d399" : "var(--color-muted)" }),
};

// ── EnvEditor ─────────────────────────────────────────────────────────────────

function EnvEditor({ env, onChange }: { env: Record<string,string>; onChange:(e:Record<string,string>)=>void }) {
  const entries = Object.entries(env);
  const set = (idx:number, k:string, v:string) => {
    const u: Record<string,string> = {};
    entries.forEach(([ek,ev],i) => { u[i===idx ? k : ek] = i===idx ? v : ev; });
    onChange(u);
  };
  return (
    <div>
      {entries.length > 0 && (
        <table style={{ width:"100%", borderCollapse:"collapse", tableLayout:"fixed", marginBottom:"0.4rem" }}>
          <colgroup>
            <col style={{ width:"33%" }} />
            <col />
            <col style={{ width:"32px" }} />
          </colgroup>
          <thead><tr>
            <th style={{ ...cx.label, display:"table-cell", padding:"0.2rem 0.4rem", textAlign:"left" }}>Key</th>
            <th style={{ ...cx.label, display:"table-cell", padding:"0.2rem 0.4rem", textAlign:"left" }}>Value</th>
            <th style={{ width:"32px" }} />
          </tr></thead>
          <tbody>
            {entries.map(([k,v],i) => (
              <tr key={i} style={{ borderBottom:"1px solid var(--color-border)" }}>
                <td style={{ padding:"0.25rem 0.3rem" }}><input style={cx.envInput} value={k} placeholder="KEY" onChange={e=>set(i,e.target.value,v)} /></td>
                <td style={{ padding:"0.25rem 0.3rem" }}><input style={cx.envInput} value={v} placeholder="value" onChange={e=>set(i,k,e.target.value)} /></td>
                <td style={{ padding:"0.25rem 0.3rem", textAlign:"center" }}><button style={cx.rmBtn} onClick={()=>{ const u={...env}; delete u[k]; onChange(u); }}>✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button style={cx.addEnvBtn} onClick={()=>onChange({...env,"":""})}>+ Add variable</button>
    </div>
  );
}

// ── EntityTable ───────────────────────────────────────────────────────────────

interface EntityRow {
  name: string;
  entity_type: string;
  key_properties: string[];
}

function EntityTable({
  discovered,
  config,
  onChange,
}: {
  discovered: EntityRow[];
  config: Record<string, EntityConfig>;
  onChange: (c: Record<string, EntityConfig>) => void;
}) {
  const allEnabled = discovered.every(e => config[e.name]?.enabled);

  function toggle(name: string, enabled: boolean) {
    onChange({ ...config, [name]: { ...config[name], enabled } });
  }
  function setField(name: string, field: "short_code" | "description", val: string) {
    onChange({ ...config, [name]: { ...config[name], [field]: val } });
  }
  function setAll(enabled: boolean) {
    const updated = { ...config };
    discovered.forEach(e => { updated[e.name] = { ...updated[e.name], enabled }; });
    onChange(updated);
  }

  const enabledCount = discovered.filter(e => config[e.name]?.enabled).length;

  return (
    <div style={cx.etWrap}>
      <div style={cx.etHdr}>
        <span style={cx.etHdrLabel}>
          Entities — {enabledCount} / {discovered.length} enabled
        </span>
        <div style={cx.etSelBtns}>
          <button style={cx.etSelBtn} onClick={() => setAll(true)}>All</button>
          <button style={cx.etSelBtn} onClick={() => setAll(false)}>None</button>
        </div>
      </div>
      <div style={{ maxHeight:"360px", overflowY:"auto" }}>
        <table style={cx.etTable}>
          <thead>
            <tr>
              <th style={{ ...cx.etTh, width:"36px", textAlign:"center" }}>On</th>
              <th style={cx.etTh}>Entity Set</th>
              <th style={{ ...cx.etTh, width:"110px" }}>Short Code</th>
              <th style={cx.etTh}>Description</th>
            </tr>
          </thead>
          <tbody>
            {discovered.map(e => {
              const cfg = config[e.name] ?? { enabled: true, short_code: "", description: "" };
              return (
                <tr key={e.name} style={{ opacity: cfg.enabled ? 1 : 0.45 }}>
                  <td style={{ ...cx.etTd, textAlign:"center" }}>
                    <input type="checkbox" checked={!!cfg.enabled}
                      onChange={ev => toggle(e.name, ev.target.checked)} />
                  </td>
                  <td style={cx.etTd}>
                    <div style={cx.etName}>{e.name}</div>
                    {e.key_properties.length > 0 && (
                      <div style={{ fontSize:"0.68rem", color:"var(--color-muted)" }}>
                        🔑 {e.key_properties.join(", ")}
                      </div>
                    )}
                  </td>
                  <td style={cx.etTd}>
                    <input style={cx.etScInput} value={cfg.short_code || ""}
                      placeholder="e.g. PO"
                      onChange={ev => setField(e.name, "short_code", ev.target.value)} />
                  </td>
                  <td style={cx.etTd}>
                    <input style={cx.etInput} value={cfg.description || ""}
                      placeholder={`${e.name} records`}
                      onChange={ev => setField(e.name, "description", ev.target.value)} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── OperationTable ── (OpenAPI operations, same EntityConfig as OData) ─────────

function OperationTable({
  operations,
  config,
  onChange,
}: {
  operations: OpenAPIOperation[];
  config: Record<string, EntityConfig>;
  onChange: (c: Record<string, EntityConfig>) => void;
}) {
  const enabledCount = operations.filter(o => config[o.operation_id]?.enabled !== false).length;

  function toggle(id: string, enabled: boolean) {
    onChange({ ...config, [id]: { ...config[id], enabled } });
  }
  function setField(id: string, field: "short_code" | "description", val: string) {
    onChange({ ...config, [id]: { ...config[id], [field]: val } });
  }
  function setAll(enabled: boolean) {
    const u = { ...config };
    operations.forEach(o => { u[o.operation_id] = { ...u[o.operation_id], enabled }; });
    onChange(u);
  }

  const methodColor: Record<string, string> = {
    GET: "#22c55e", POST: "#FF6012", PUT: "#456DE6",
    PATCH: "#f5a623", DELETE: "#ef4444", OPTIONS: "#71717a", HEAD: "#71717a",
  };

  // Group by tag
  const groups: Record<string, OpenAPIOperation[]> = {};
  operations.forEach(o => {
    const tag = o.tags[0] || "default";
    (groups[tag] = groups[tag] || []).push(o);
  });

  return (
    <div style={cx.etWrap}>
      <div style={cx.etHdr}>
        <span style={cx.etHdrLabel}>
          Operations — {enabledCount} / {operations.length} enabled
        </span>
        <div style={cx.etSelBtns}>
          <button style={cx.etSelBtn} onClick={() => setAll(true)}>All</button>
          <button style={cx.etSelBtn} onClick={() => setAll(false)}>None</button>
        </div>
      </div>
      <div style={{ maxHeight:"420px", overflowY:"auto" }}>
        <table style={cx.etTable}>
          <thead>
            <tr>
              <th style={{ ...cx.etTh, width:"36px", textAlign:"center" as const }}>On</th>
              <th style={{ ...cx.etTh, width:"70px" }}>Method</th>
              <th style={cx.etTh}>Path / Operation ID</th>
              <th style={{ ...cx.etTh, width:"110px" }}>Short Code</th>
              <th style={cx.etTh}>Description / Summary</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(groups).map(([tag, ops]) => (
              <React.Fragment key={tag}>
                <tr>
                  <td colSpan={5} style={{ ...cx.etTd, background:"var(--input-bg)", fontSize:"10px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.08em", color:"var(--fg-muted)", padding:"5px 10px" }}>
                    {tag}
                  </td>
                </tr>
                {ops.map(op => {
                  const cfg = config[op.operation_id] ?? { enabled: true, short_code: "", description: "" };
                  return (
                    <tr key={op.operation_id} style={{ opacity: cfg.enabled !== false ? 1 : 0.4 }}>
                      <td style={{ ...cx.etTd, textAlign:"center" as const }}>
                        <input type="checkbox" checked={cfg.enabled !== false}
                          onChange={ev => toggle(op.operation_id, ev.target.checked)} />
                      </td>
                      <td style={cx.etTd}>
                        <span style={{ fontFamily:"monospace", fontSize:"11px", fontWeight:700, padding:"2px 6px", borderRadius:"4px", background:`${(methodColor[op.method]||"#71717a")}22`, color: methodColor[op.method]||"#71717a" }}>
                          {op.method}
                        </span>
                      </td>
                      <td style={cx.etTd}>
                        <div style={{ fontFamily:"monospace", fontSize:"12px", color:"var(--fg)", wordBreak:"break-all" as const }}>{op.path}</div>
                        <div style={{ fontSize:"11px", color:"var(--fg-muted)" }}>{op.operation_id}</div>
                      </td>
                      <td style={cx.etTd}>
                        <input style={cx.etScInput} value={cfg.short_code || ""}
                          placeholder="alias"
                          onChange={ev => setField(op.operation_id, "short_code", ev.target.value)} />
                      </td>
                      <td style={cx.etTd}>
                        <input style={cx.etInput} value={cfg.description || ""}
                          placeholder={op.summary || `${op.method} ${op.path}`}
                          onChange={ev => setField(op.operation_id, "description", ev.target.value)} />
                      </td>
                    </tr>
                  );
                })}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Toggle ────────────────────────────────────────────────────────────────────

function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  const on = checked;
  return (
    <div style={{ marginBottom:"14px" }}>
      <div style={{ display:"flex", alignItems:"center", gap:"10px", cursor:"pointer" }} onClick={() => onChange(!on)}>
        <div style={{ width:"40px", height:"22px", borderRadius:"11px", background: on ? "var(--fg-muted)" : "var(--border)", position:"relative", transition:"background .2s", flexShrink:0 }}>
          <div style={{ position:"absolute", top:"3px", left: on ? "21px" : "3px", width:"16px", height:"16px", borderRadius:"50%", background:"#fff", transition:"left .2s" }} />
        </div>
        <span style={{ fontSize:"13px", fontWeight:600, color:"var(--fg)" }}>{label}</span>
      </div>
      {hint && <div style={{ fontSize:"11px", color:"var(--fg-muted)", marginTop:"3px", marginLeft:"50px" }}>{hint}</div>}
    </div>
  );
}

// ── ODataForm ─────────────────────────────────────────────────────────────────

function ODataForm({ form, setForm, discovering, discoverError, discoveredEntities, onDiscover }: {
  form: EditState;
  setForm: React.Dispatch<React.SetStateAction<EditState>>;
  discovering: boolean;
  discoverError: string;
  discoveredEntities: ODataEntity[];
  onDiscover: () => void;
}) {
  const f = (field: keyof EditState, val: any) => setForm(p => ({ ...p, [field]: val }));
  const fOps = (op: string, val: boolean) => setForm(p => ({ ...p, operations: { ...p.operations, [op]: val } }));

  // Build live payload JSON preview
  const payload = {
    serviceUrl: form.url,
    metadataUrl: form.metadata_url || `${form.url}/$metadata`,
    metadataContent: form.metadata_content || "",
    version: form.odata_version === "V4" ? "VERSION_V4" : "VERSION_V2",
    operations: form.operations,
    nameOverrides: form.name_overrides,
    defaultPageSize: form.default_page_size,
    maxPageSize: form.max_page_size,
    enableCsrf: form.enable_csrf,
    basic: { username: form.auth?.username || "", passwordSecretRef: "" },
  };

  const iS = {
    twoCol: { display:"grid", gridTemplateColumns:"1fr 320px", gap:"24px", alignItems:"start" } as React.CSSProperties,
    section: { marginBottom:"20px" } as React.CSSProperties,
    sectionTitle: { fontSize:"16px", fontWeight:700, color:"var(--fg)", margin:"0 0 4px" } as React.CSSProperties,
    sectionSub: { fontSize:"12px", color:"var(--fg-muted)", marginBottom:"14px" } as React.CSSProperties,
    card: { background:"var(--card)", border:"1px solid var(--border)", borderRadius:"8px", padding:"16px", marginBottom:"12px" } as React.CSSProperties,
    label: { display:"block" as const, fontSize:"11px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.07em", color:"var(--fg-muted)", marginBottom:"4px" },
    input: { width:"100%", background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"7px 10px", color:"var(--fg)", fontSize:"13px", fontFamily:"monospace" } as React.CSSProperties,
    textarea: { width:"100%", background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"8px 10px", color:"var(--fg)", fontSize:"12px", fontFamily:"monospace", resize:"vertical" as const, minHeight:"80px" } as React.CSSProperties,
    hint: { fontSize:"11px", color:"var(--fg-muted)", marginTop:"3px" },
    sectionH: { fontSize:"11px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.08em", color:"var(--fg-muted)", margin:"16px 0 8px" },
    previewCard: { background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"8px", padding:"16px", position:"sticky" as const, top:"16px" },
    previewTitle: { fontSize:"12px", fontWeight:700, color:"var(--fg)", marginBottom:"6px" },
    previewSub: { fontSize:"11px", color:"var(--fg-muted)", marginBottom:"10px" },
    pre: { fontSize:"11px", lineHeight:1.6, fontFamily:"monospace", color:"#a9b1d6", overflowX:"auto" as const, whiteSpace:"pre" as const, maxHeight:"600px", overflowY:"auto" as const },
    tagGroup: { display:"flex", flexWrap:"wrap" as const, gap:"6px", marginTop:"6px" },
    tag: { display:"inline-flex", alignItems:"center", gap:"4px", background:"rgba(255,255,255,0.06)", border:"1px solid var(--border)", borderRadius:"4px", padding:"2px 8px", fontSize:"12px", fontFamily:"monospace", color:"var(--fg)" },
    removeTag: { background:"transparent", border:"none", color:"var(--fg-muted)", cursor:"pointer", fontSize:"12px", padding:"0", lineHeight:1 },
    addSetBtn: { background:"transparent", border:"1px dashed var(--border)", borderRadius:"5px", padding:"4px 12px", fontSize:"12px", color:"var(--fg-muted)", cursor:"pointer", marginTop:"4px" },
    overrideRow: { display:"grid", gridTemplateColumns:"1fr 1fr 28px", gap:"6px", alignItems:"center", marginBottom:"6px" } as React.CSSProperties,
    discoverBtn: (dis: boolean): React.CSSProperties => ({ padding:"7px 16px", background: dis ? "var(--btn-bg-disabled)" : "var(--btn-bg)", color: dis ? "var(--btn-fg-disabled)" : "var(--btn-fg)", border:"none", borderRadius:"6px", fontWeight:700, fontSize:"13px", cursor: dis ? "not-allowed" : "pointer" }),
  };

  function addInclude() { f("include_sets", [...(form.include_sets||[]), ""]); }
  function addExclude() { f("exclude_sets", [...(form.exclude_sets||[]), ""]); }
  function addOverride() { setForm(p => ({ ...p, name_overrides: { ...p.name_overrides, "": "" } })); }

  return (
    <div style={iS.twoCol}>
      {/* ── LEFT: form ── */}
      <div>
        {/* General + Connection (merged) */}
        <div style={iS.card}>
          <h3 style={iS.sectionTitle}>OData</h3>
          <p style={iS.sectionSub}>Generate MCP tools from an OData service's $metadata (EDMX), e.g. SAP Gateway.</p>

          <div style={{ marginBottom:"14px" }}>
            <label style={iS.label}>Description</label>
            <input style={iS.input} value={form.description||""} placeholder="SAP Purchase Order OData"
              onChange={e => f("description", e.target.value)} />
            <div style={iS.hint}>Optional human-readable description.</div>
          </div>

          <div style={iS.sectionH}>Service URL <span style={{ color:"#ef4444" }}>*</span></div>
          <input style={iS.input} value={form.url}
            placeholder="https://cpidev.apimanagement.us21.hana.ondemand.com/v2/Purchase_Order"
            onChange={e => f("url", e.target.value)} />
          <div style={iS.hint}>OData service root URL. $metadata is read from &lt;service_url&gt;/$metadata unless overridden below.</div>

          <div style={{ ...iS.sectionH, marginTop:"16px" }}>Metadata URL</div>
          <input style={iS.input} value={form.metadata_url||""}
            placeholder={form.url ? `${form.url}/$metadata` : "https://…/$metadata"}
            onChange={e => f("metadata_url", e.target.value)} />
          <div style={iS.hint}>Optional. URL to the EDMX $metadata document if not at &lt;service_url&gt;/$metadata.</div>

          <div style={{ ...iS.sectionH, marginTop:"16px" }}>Metadata Content</div>
          <textarea style={iS.textarea} value={form.metadata_content||""}
            placeholder=".xml,.edmx,application/xml,text/xml"
            onChange={e => f("metadata_content", e.target.value)} />
          <div style={iS.hint}>Paste the EDMX $metadata XML, or load it from a file. 3 MiB max — larger documents should use a URL instead.</div>

          <div style={{ ...iS.sectionH, marginTop:"16px" }}>Version</div>
          {(["V2","V4"] as const).map(v => (
            <label key={v} style={{ display:"flex", alignItems:"center", gap:"8px", marginBottom:"6px", cursor:"pointer", fontSize:"13px" }}>
              <input type="radio" checked={(form.odata_version||"V2")===v} onChange={()=>f("odata_version",v)} />
              {v}
            </label>
          ))}
          <div style={iS.hint}>OData protocol version. Auto-detected from $metadata when unspecified.</div>

          <div style={{ marginTop:"16px", borderTop:"1px solid var(--border)", paddingTop:"14px" }}>
            <Toggle checked={form.enable_csrf} onChange={v=>f("enable_csrf",v)} label="Enable CSRF"
              hint="Automate the SAP X-CSRF-Token fetch/echo handshake on writes. Enable for SAP Gateway OData v2." />
          </div>
        </div>

        {/* Discover */}
        <div style={{ display:"flex", alignItems:"center", gap:"10px", marginBottom:"12px" }}>
          <button style={iS.discoverBtn(discovering||!form.url)} disabled={discovering||!form.url} onClick={onDiscover}>
            {discovering ? "⠋ Discovering…" : "🔍 Discover Entities"}
          </button>
          {discoverError && <span style={{ color:"#f87171", fontSize:"12px" }}>✗ {discoverError}</span>}
          {discoveredEntities.length>0 && !discoverError && (
            <span style={{ color:"#34d399", fontSize:"12px" }}>✓ {discoveredEntities.length} entities found</span>
          )}
        </div>

        {/* Filter */}
        <div style={iS.card}>
          <h3 style={iS.sectionTitle}>Filter</h3>
          <p style={iS.sectionSub}>Filter which entity sets are exposed as MCP tools.</p>

          <div style={iS.sectionH}>Include Entity Sets</div>
          <div style={iS.tagGroup}>
            {(form.include_sets||[]).map((s,i) => (
              <span key={i} style={iS.tag}>
                <input value={s} style={{ background:"transparent", border:"none", outline:"none", color:"var(--fg)", fontFamily:"monospace", fontSize:"12px", width:`${Math.max(s.length,8)}ch` }}
                  onChange={e => { const a=[...(form.include_sets||[])]; a[i]=e.target.value; f("include_sets",a); }} />
                <button style={iS.removeTag} onClick={()=>{ const a=(form.include_sets||[]).filter((_,j)=>j!==i); f("include_sets",a); }}>✕</button>
              </span>
            ))}
          </div>
          <button style={iS.addSetBtn} onClick={addInclude}>+ Add Include Entity Sets</button>
          <div style={iS.hint}>Glob patterns matched against entity-set names to include.</div>

          <div style={{ ...iS.sectionH, marginTop:"14px" }}>Exclude Entity Sets</div>
          <div style={iS.tagGroup}>
            {(form.exclude_sets||[]).map((s,i) => (
              <span key={i} style={iS.tag}>
                <input value={s} style={{ background:"transparent", border:"none", outline:"none", color:"var(--fg)", fontFamily:"monospace", fontSize:"12px", width:`${Math.max(s.length,8)}ch` }}
                  onChange={e => { const a=[...(form.exclude_sets||[])]; a[i]=e.target.value; f("exclude_sets",a); }} />
                <button style={iS.removeTag} onClick={()=>{ const a=(form.exclude_sets||[]).filter((_,j)=>j!==i); f("exclude_sets",a); }}>✕</button>
              </span>
            ))}
          </div>
          <button style={iS.addSetBtn} onClick={addExclude}>+ Add Exclude Entity Sets</button>
          <div style={iS.hint}>Glob patterns matched against entity-set names to exclude.</div>
        </div>

        {/* Operations */}
        <div style={iS.card}>
          <h3 style={iS.sectionTitle}>Operations</h3>
          <p style={iS.sectionSub}>Which operations to expose per entity set. Defaults to read-only (query + get).</p>
          {([
            ["query",  "Query",  "Expose a list/filter tool per entity set."],
            ["get",    "Get",    "Expose a get-by-key tool per entity set."],
            ["count",  "Count",  "Expose a count tool per entity set."],
            ["create", "Create", "Expose a create (POST) tool. Mutating — gate with policy."],
            ["update", "Update", "Expose an update (PATCH) tool. Mutating — gate with policy."],
            ["delete", "Delete", "Expose a delete tool. Mutating — gate with policy."],
          ] as [string, string, string][]).map(([key, label, hint]) => (
            <Toggle key={key} checked={!!(form.operations||{})[key as keyof typeof form.operations]}
              onChange={v => fOps(key, v)} label={label} hint={hint} />
          ))}
        </div>

        {/* Name Overrides */}
        <div style={iS.card}>
          <h3 style={iS.sectionTitle}>Name Overrides</h3>
          <p style={iS.sectionSub}>Pin a tool-name prefix for an entity set, e.g. A_PurchaseRequisitionItem → pr_item.</p>
          {Object.entries(form.name_overrides||{}).map(([k,v],i) => (
            <div key={i} style={iS.overrideRow}>
              <input style={iS.input} value={k} placeholder="EntitySetName"
                onChange={e => {
                  const u={...form.name_overrides}; delete u[k];
                  u[e.target.value]=v; f("name_overrides",u);
                }} />
              <input style={iS.input} value={v} placeholder="short_name"
                onChange={e => { const u={...form.name_overrides}; u[k]=e.target.value; f("name_overrides",u); }} />
              <button style={{ background:"transparent", border:"none", color:"#f87171", cursor:"pointer", fontSize:"14px" }}
                onClick={()=>{ const u={...form.name_overrides}; delete u[k]; f("name_overrides",u); }}>✕</button>
            </div>
          ))}
          <button style={iS.addSetBtn} onClick={addOverride}>+ Add pair</button>
        </div>

        {/* Pagination */}
        <div style={iS.card}>
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"12px" }}>
            <div>
              <label style={iS.label}>Default Page Size</label>
              <input style={iS.input} type="number" value={form.default_page_size||0}
                onChange={e => f("default_page_size", parseInt(e.target.value)||0)} />
              <div style={iS.hint}>Default $top page size when a query omits one. 0 uses the built-in default (50).</div>
            </div>
            <div>
              <label style={iS.label}>Max Page Size</label>
              <input style={iS.input} type="number" value={form.max_page_size||0}
                onChange={e => f("max_page_size", parseInt(e.target.value)||0)} />
              <div style={iS.hint}>Upper bound on $top. Larger requests are clamped. 0 uses the built-in default (500).</div>
            </div>
          </div>
        </div>

        {/* Auth */}
        <div style={iS.card}>
          <h3 style={iS.sectionTitle}>Auth <span style={{ color:"#ef4444" }}>*</span></h3>
          <div style={{ marginBottom:"12px" }}>
            <label style={iS.label}>Auth Type</label>
            <select style={{ ...iS.input, cursor:"pointer" }} value={form.auth?.type||"basic"}
              onChange={e => setForm(p=>({...p,auth:{...p.auth,type:e.target.value}}))}>
              <option value="basic">Basic</option>
              <option value="apikey">API Key</option>
              <option value="none">None</option>
            </select>
          </div>
          {(form.auth?.type||"basic") === "basic" && (<>
            <h4 style={{ fontSize:"14px", fontWeight:600, color:"var(--fg)", margin:"0 0 10px" }}>Basic</h4>
            <div style={{ marginBottom:"12px" }}>
              <label style={iS.label}>Username <span style={{ color:"#ef4444" }}>*</span></label>
              <input style={iS.input} value={form.auth?.username||""} placeholder="RFCAPIMTS410"
                onChange={e => setForm(p=>({...p,auth:{...p.auth,username:e.target.value}}))} />
              <div style={iS.hint}>Username for HTTP Basic authentication.</div>
            </div>
            <div>
              <label style={iS.label}>Password <span style={{ color:"#ef4444" }}>*</span></label>
              <input style={iS.input} type="password" value={form.auth?.password||""} placeholder="••••••••"
                onChange={e => setForm(p=>({...p,auth:{...p.auth,password:e.target.value}}))} />
            </div>
          </>)}
          {(form.auth?.type) === "apikey" && (
            <div>
              <label style={iS.label}>API Key</label>
              <input style={iS.input} type="password" value={form.auth?.api_key||""}
                onChange={e => setForm(p=>({...p,auth:{...p.auth,api_key:e.target.value}}))} />
            </div>
          )}
        </div>

        {/* Entity table (from discover) */}
        {discoveredEntities.length > 0 && (
          <div style={iS.card}>
            <h3 style={iS.sectionTitle}>Entity Selection</h3>
            <p style={iS.sectionSub}>Only enabled entities are included when invoking the LLM.</p>
            <EntityTable discovered={discoveredEntities} config={form.entities}
              onChange={entities => setForm(f=>({...f,entities}))} />
          </div>
        )}
      </div>

      {/* ── RIGHT: payload preview ── */}
      <div style={iS.previewCard}>
        <div style={iS.previewTitle}>Payload JSON</div>
        <div style={iS.previewSub}>Shows the config body that will be saved. Use it to sanity-check the final payload.</div>
        <pre style={iS.pre}>{JSON.stringify(payload, null, 2)}</pre>
      </div>
    </div>
  );
}

// ── McpEditor ─────────────────────────────────────────────────────────────────

function McpEditor({
  server, onSaved, onDeleted, onCopy,
}: {
  server: EditState;
  onSaved: (name: string, s: EditState) => void;
  onDeleted: (name: string) => void;
  onCopy: (s: EditState) => void;
}) {
  const [form, setForm] = useState<EditState>({ ...server });
  const [cmdText, setCmdText] = useState(cmdStr(server.command));
  const [argsText, setArgsText] = useState(server.args.join(" "));
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [discoveredEntities, setDiscoveredEntities] = useState<ODataEntity[]>([]);
  const [discoveredOperations, setDiscoveredOperations] = useState<OpenAPIOperation[]>([]);
  const [discoverError, setDiscoverError] = useState("");
  const [specTitle, setSpecTitle] = useState("");
  const [toast, setToast] = useState<{ type:"success"|"error"; msg:string }|null>(null);

  useEffect(() => {
    setForm({ ...server, auth: migrateAuth(server.auth) });
    setCmdText(cmdStr(server.command));
    setArgsText(server.args.join(" "));
    setDiscoveredEntities([]);
    setDiscoveredOperations([]);
    setDiscoverError("");
    setSpecTitle("");
  }, [server.name, server._isNew]);

  // Pre-populate discovered list from saved entities (OData)
  useEffect(() => {
    if (form.type === "odata" && Object.keys(form.entities).length > 0 && discoveredEntities.length === 0) {
      const synth: ODataEntity[] = Object.keys(form.entities).map(name => ({
        name, entity_type: name, key_properties: [], properties: [],
      }));
      setDiscoveredEntities(synth);
    }
    if (form.type === "openapi" && Object.keys(form.entities).length > 0 && discoveredOperations.length === 0) {
      const synth: OpenAPIOperation[] = Object.keys(form.entities).map(id => ({
        operation_id: id, method: "GET", path: id, summary: form.entities[id]?.description || "", tags: [],
      }));
      setDiscoveredOperations(synth);
    }
  }, [form.type]);

  function showToast(type:"success"|"error", msg:string) {
    setToast({ type, msg });
    setTimeout(() => setToast(null), 3500);
  }

  async function handleDiscover() {
    setDiscovering(true);
    setDiscoverError("");
    try {
      const data = await api.discoverOData({
        url: form.url,
        service: form.service,
        username: form.auth?.username || "",
        password: form.auth?.password || "",
      });
      const entities: ODataEntity[] = data.entities || [];
      setDiscoveredEntities(entities);
      const updated = { ...form.entities };
      entities.forEach(e => {
        if (!updated[e.name]) updated[e.name] = { enabled: true, short_code: "", description: "" };
      });
      setForm(f => ({ ...f, entities: updated }));
      showToast("success", `Discovered ${entities.length} entities`);
    } catch (e: any) {
      setDiscoverError(e.message);
    }
    setDiscovering(false);
  }

  async function handleDiscoverOpenAPI() {
    setDiscovering(true);
    setDiscoverError("");
    try {
      const ma   = migrateAuth(form.auth);
      const creds = resolveAuthCreds(ma);
      const data = await api.discoverOpenAPI({
        url: form.url,
        auth_type:        ma.type,
        api_key:          creds.api_key,
        api_key_header:   creds.api_key_header,
        username:         creds.username,
        password:         creds.password,
        oauth_client_id:     creds.oauth_client_id,
        oauth_client_secret: creds.oauth_client_secret,
        oauth_token_url:     creds.oauth_token_url,
        oauth_scope:         creds.oauth_scope,
      });
      const ops: OpenAPIOperation[] = data.operations || [];
      setDiscoveredOperations(ops);
      setSpecTitle(`${data.title || ""} ${data.version ? "v" + data.version : ""}`.trim());
      // Seed operation configs
      const updated = { ...form.entities };
      ops.forEach(op => {
        if (!updated[op.operation_id]) {
          updated[op.operation_id] = { enabled: true, short_code: "", description: op.summary || "" };
        }
      });
      setForm(f => ({ ...f, entities: updated }));
      showToast("success", `Discovered ${ops.length} operations`);
    } catch (e: any) {
      setDiscoverError(e.message);
    }
    setDiscovering(false);
  }

  async function handleSave() {
    const payload: Record<string, unknown> = {
      type: form.type,
      enabled: form.enabled,
    };
    if (form.type === "stdio") {
      payload.command = cmdArr(cmdText);
      payload.args = argsText.trim() ? argsText.trim().split(/\s+/) : [];
      payload.env = form.env;
    } else if (form.type === "openapi") {
      payload.url = form.url;
      payload.auth = migrateAuth(form.auth);   // save full MultiAuth structure
      payload.entities = form.entities;
      payload.env = form.env;
    } else if (form.type === "odata") {
      payload.description = form.description;
      payload.code_mode = form.code_mode;
      payload.url = form.url;
      payload.service = form.service;
      payload.metadata_url = form.metadata_url;
      payload.metadata_content = form.metadata_content;
      payload.odata_version = form.odata_version;
      payload.include_sets = form.include_sets;
      payload.exclude_sets = form.exclude_sets;
      payload.operations = form.operations;
      payload.name_overrides = form.name_overrides;
      payload.default_page_size = form.default_page_size;
      payload.max_page_size = form.max_page_size;
      payload.enable_csrf = form.enable_csrf;
      payload.auth = { type: "basic", username: form.auth?.username || "", password: form.auth?.password || "" };
      payload.entities = form.entities;
    }
    setSaving(true);
    try {
      if (form._isNew) {
        await api.createMcpServer({ name: form.name, ...payload });
      } else {
        await api.saveMcpServer(form.name, payload);
      }
      showToast("success", `Saved: ${form.name}`);
      onSaved(form.name, { ...form, ...(payload as any), _isNew: false });
    } catch (e: any) {
      showToast("error", e.message);
    }
    setSaving(false);
  }

  async function handleDelete() {
    if (!confirm(`Delete MCP server "${form.name}"?`)) return;
    try {
      await api.deleteMcpServer(form.name);
      onDeleted(form.name);
    } catch (e: any) {
      showToast("error", e.message);
    }
  }

  function handleCopy() {
    const copyName = `${form.name}_copy`;
    onCopy({ ...form, name: copyName, _isNew: true, connected: false, tools: [] });
  }

  const connBadge = !form._isNew && (
    <span style={cx.badge(form.connected)}>{form.connected ? "● connected" : "○ disconnected"}</span>
  );

  return (
    <div style={cx.card}>
      {/* Header */}
      <div style={cx.cardH}>
        <h2 style={cx.h2}>
          🔌 {form._isNew ? "New MCP Server" : form.name}
          {connBadge}
          {form.tools.length > 0 && (
            <span style={{ fontSize:"0.72rem", padding:"0.12rem 0.5rem", borderRadius:"10px", background:"var(--sidebar-active)", color:"var(--fg-muted)", fontWeight:600 }}>
              {form.tools.length} tools
            </span>
          )}
        </h2>
        <label style={{ display:"flex", alignItems:"center", gap:"0.4rem", fontSize:"0.84rem", color:"var(--color-muted)", cursor:"pointer" }}>
          <input type="checkbox" checked={form.enabled} onChange={e => setForm(f=>({...f,enabled:e.target.checked}))} />
          Enabled
        </label>
        {form.type === "odata" && (
          <label style={{ display:"flex", alignItems:"center", gap:"0.4rem", fontSize:"0.84rem", color:"var(--color-muted)", cursor:"pointer", marginLeft:"12px" }}>
            <input type="checkbox" checked={!!(form as any).code_mode} onChange={e => setForm(f=>({...f,code_mode:e.target.checked}))} />
            Code Mode
          </label>
        )}
      </div>

      {/* Name */}
      {form._isNew && (
        <div style={cx.field}>
          <label style={cx.label}>Server Name</label>
          <input style={cx.input} value={form.name} placeholder="my-sap-odata"
            onChange={e => setForm(f=>({...f,name:e.target.value}))} />
        </div>
      )}

      {/* Type selector */}
      <div style={{ marginBottom:"1.25rem" }}>
        <label style={cx.label}>Server Type</label>
        <div style={cx.typeSel}>
          {(["stdio","openapi","odata"] as ServerType[]).map(t => (
            <button key={t} style={cx.typeBtn(form.type===t)}
              onClick={() => setForm(f=>({...f,type:t}))}>
              {t === "stdio" ? "⌨ stdio" : t === "openapi" ? "📄 OpenAPI" : "🏭 OData"}
            </button>
          ))}
        </div>
      </div>

      {/* ── stdio form ── */}
      {form.type === "stdio" && (<>
        <div style={cx.field}>
          <label style={cx.label}>Command</label>
          <input style={cx.input} value={cmdText} placeholder="npx -y @modelcontextprotocol/server-filesystem"
            onChange={e => setCmdText(e.target.value)} />
          <div style={cx.hint}>Executable and flags, space-separated</div>
        </div>
        <div style={cx.field}>
          <label style={cx.label}>Arguments</label>
          <input style={cx.input} value={argsText} placeholder="/path/to/workspace"
            onChange={e => setArgsText(e.target.value)} />
        </div>
        <div style={cx.field}>
          <label style={cx.label}>Environment Variables</label>
          <EnvEditor env={form.env} onChange={env => setForm(f=>({...f,env}))} />
        </div>
      </>)}

      {/* ── openapi form ── */}
      {form.type === "openapi" && (<>
        <hr style={cx.divider} />
        <div style={cx.sectionH}>Connection</div>

        <div style={cx.field}>
          <label style={cx.label}>OpenAPI Spec URL</label>
          <input style={cx.input} value={form.url}
            placeholder="https://api.example.com/openapi.json"
            onChange={e => setForm(f=>({...f,url:e.target.value}))} />
          <div style={cx.hint}>URL to OpenAPI 3.x JSON or YAML spec</div>
        </div>

        <div style={cx.row2}>
          <div style={cx.field}>
            <label style={cx.label}>Auth Type</label>
            <select style={{ ...cx.input, cursor:"pointer" }}
              value={migrateAuth(form.auth).type}
              onChange={e => setForm(f => ({ ...f, auth: switchAuthType(migrateAuth(f.auth), e.target.value as AuthType) }))}>
              <option value="none">No Auth</option>
              <option value="basic">Basic Auth</option>
              <option value="apikey">API Key</option>
              <option value="oauth">OAuth 2.0</option>
            </select>
          </div>
          {migrateAuth(form.auth).type === "apikey" && (
            <div style={cx.field}>
              <label style={cx.label}>Header Name</label>
              <input style={cx.input} value={migrateAuth(form.auth).apikey.api_key_header || "Authorization"}
                placeholder="Authorization"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, apikey: { ...ma.apikey, api_key_header: e.target.value } } }; })} />
            </div>
          )}
        </div>

        {migrateAuth(form.auth).type === "apikey" && (
          <div style={cx.field}>
            <label style={cx.label}>API Key</label>
            <input style={cx.input} type="password" value={migrateAuth(form.auth).apikey.api_key || ""}
              placeholder="your-api-key"
              onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, apikey: { ...ma.apikey, api_key: e.target.value } } }; })} />
          </div>
        )}
        {migrateAuth(form.auth).type === "basic" && (
          <div style={cx.row2}>
            <div style={cx.field}>
              <label style={cx.label}>Username</label>
              <input style={cx.input} value={migrateAuth(form.auth).basic.username || ""} placeholder="username"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, basic: { ...ma.basic, username: e.target.value } } }; })} />
            </div>
            <div style={cx.field}>
              <label style={cx.label}>Password</label>
              <input style={cx.input} type="password" value={migrateAuth(form.auth).basic.password || ""}
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, basic: { ...ma.basic, password: e.target.value } } }; })} />
            </div>
          </div>
        )}
        {migrateAuth(form.auth).type === "oauth" && (<>
          <div style={cx.row2}>
            <div style={cx.field}>
              <label style={cx.label}>Client ID</label>
              <input style={cx.input} value={migrateAuth(form.auth).oauth.client_id || ""} placeholder="client_id"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, oauth: { ...ma.oauth, client_id: e.target.value } } }; })} />
            </div>
            <div style={cx.field}>
              <label style={cx.label}>Client Secret</label>
              <input style={cx.input} type="password" value={migrateAuth(form.auth).oauth.client_secret || ""} placeholder="client_secret"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, oauth: { ...ma.oauth, client_secret: e.target.value } } }; })} />
            </div>
          </div>
          <div style={cx.row2}>
            <div style={cx.field}>
              <label style={cx.label}>Token URL</label>
              <input style={cx.input} value={migrateAuth(form.auth).oauth.token_url || ""} placeholder="https://auth.example.com/oauth/token"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, oauth: { ...ma.oauth, token_url: e.target.value } } }; })} />
            </div>
            <div style={cx.field}>
              <label style={cx.label}>Scope (optional)</label>
              <input style={cx.input} value={migrateAuth(form.auth).oauth.scope || ""} placeholder="read write"
                onChange={e => setForm(f => { const ma = migrateAuth(f.auth); return { ...f, auth: { ...ma, oauth: { ...ma.oauth, scope: e.target.value } } }; })} />
            </div>
          </div>
        </>)}

        {/* Discover button */}
        <div style={{ display:"flex", alignItems:"center", gap:"0.75rem", marginBottom:"1.25rem" }}>
          <button style={cx.discoverBtn(discovering || !form.url)} disabled={discovering || !form.url}
            onClick={handleDiscoverOpenAPI}>
            {discovering ? "⠋ Discovering…" : "🔍 Discover Operations"}
          </button>
          {discoverError && <span style={{ color:"#f87171", fontSize:"0.82rem" }}>✗ {discoverError}</span>}
          {discoveredOperations.length > 0 && !discoverError && (
            <span style={{ color:"#34d399", fontSize:"0.82rem" }}>
              ✓ {discoveredOperations.length} operations{specTitle ? ` — ${specTitle}` : ""}
            </span>
          )}
        </div>

        {/* Operations table */}
        {discoveredOperations.length > 0 && (<>
          <hr style={cx.divider} />
          <div style={cx.sectionH}>Operations</div>
          <div style={{ fontSize:"0.78rem", color:"var(--fg-muted)", marginBottom:"0.75rem" }}>
            Only <strong style={{ color:"var(--color-foreground)" }}>enabled</strong> operations are exposed to the LLM.
            Set a short code and description to help the model understand each operation.
          </div>
          <OperationTable
            operations={discoveredOperations}
            config={form.entities}
            onChange={entities => setForm(f=>({...f,entities}))}
          />
        </>)}

        <div style={cx.field}>
          <label style={cx.label}>Environment Variables</label>
          <EnvEditor env={form.env} onChange={env => setForm(f=>({...f,env}))} />
        </div>
      </>)}

      {/* ── odata form ── */}
      {form.type === "odata" && (
        <ODataForm form={form} setForm={setForm}
          discovering={discovering} discoverError={discoverError}
          discoveredEntities={discoveredEntities}
          onDiscover={handleDiscover} />
      )}

      {/* Registered tools (for connected servers) */}
      {form.tools.length > 0 && (
        <div style={{ marginTop:"1.25rem" }}>
          <label style={cx.label}>Registered Tools</label>
          <div style={{ display:"flex", flexWrap:"wrap", gap:"0.3rem" }}>
            {form.tools.map(t => (
              <span key={t} style={{ background:"var(--color-background)", border:"1px solid var(--color-border)", borderRadius:"4px", padding:"0.12rem 0.45rem", fontSize:"0.76rem", fontFamily:"monospace" }}>{t}</span>
            ))}
          </div>
        </div>
      )}

      <div style={cx.actions}>
        <button style={cx.saveBtn(saving)} disabled={saving} onClick={handleSave}>
          {saving ? "⠋ Saving…" : "💾 Save"}
        </button>
        {!form._isNew && (
          <button
            style={{ background:"transparent", color:"var(--color-muted)", border:"1px solid var(--color-border)", borderRadius:"6px", padding:"0.5rem 1rem", fontSize:"0.875rem", cursor:"pointer" }}
            onClick={handleCopy}
            title={`Copy as ${form.name}_copy`}
          >
            📋 Copy
          </button>
        )}
        {!form._isNew && (
          <button style={cx.delBtn} onClick={handleDelete}>🗑 Delete</button>
        )}
      </div>

      {toast && <div style={cx.toast(toast.type)}>{toast.msg}</div>}
    </div>
  );
}

// ── McpPage ───────────────────────────────────────────────────────────────────

export function McpPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<EditState | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getMcpServers();
      setServers(data.servers || []);
    } catch { setServers([]); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const typeIcon = (t: string) => t === "odata" ? "🏭" : t === "openapi" ? "📄" : "⌨";

  if (loading) return <div style={{ padding:"2rem", color:"var(--color-muted)" }}>⠋ Loading MCP servers…</div>;

  return (
    <div style={cx.layout}>
      {/* Sidebar */}
      <div style={cx.sidebar}>
        <div style={cx.sbHeader}>
          <p style={cx.sbTitle}>MCP Servers</p>
          <button style={cx.addBtn} onClick={() => setSelected(emptyServer())}>+ New</button>
        </div>
        <div style={cx.list}>
          {servers.length === 0 && (
            <div style={{ padding:"1rem", color:"var(--color-muted)", fontSize:"0.84rem" }}>No MCP servers configured.</div>
          )}
          {servers.map(s => {
            const isActive = selected?.name === s.name && !selected?._isNew;
            const enabledCount = s.type === "odata"
              ? Object.values(s.entities || {}).filter((e: any) => e.enabled).length
              : null;
            return (
              <div key={s.name} style={cx.item(isActive)}
                onClick={() => setSelected({ ...s, _isNew: false })}>
                <div style={{ display:"flex", alignItems:"center" }}>
                  <span style={cx.dot(s.connected)} />
                  <span style={cx.itemName(isActive)}>{typeIcon(s.type)} {s.name}</span>
                </div>
                <div style={{ fontSize:"0.7rem", color:"var(--color-muted)", paddingLeft:"1rem", marginTop:"0.1rem" }}>
                  {s.type === "odata"
                    ? `OData · ${enabledCount} entities`
                    : s.type === "openapi"
                    ? "OpenAPI"
                    : `stdio${s.tools.length > 0 ? ` · ${s.tools.length} tools` : ""}`}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Main */}
      <div style={cx.main}>
        {!selected ? (
          <div style={cx.empty}>
            <span style={{ fontSize:"2rem" }}>🔌</span>
            <span>Select a server or click <strong>+ New</strong></span>
            <div style={{ fontSize:"0.78rem", color:"var(--color-muted)", maxWidth:"340px", textAlign:"center" }}>
              Supports <strong>stdio</strong> MCP servers, <strong>OpenAPI</strong> specs, and <strong>OData</strong> services with entity discovery.
            </div>
          </div>
        ) : (
          <McpEditor
            key={selected.name + String(selected._isNew)}
            server={selected}
            onSaved={(name, data) => {
              setServers(prev => {
                const exists = prev.find(s => s.name === name);
                return exists ? prev.map(s => s.name === name ? { ...s, ...data } : s) : [...prev, { ...data }];
              });
              setSelected({ ...data, _isNew: false });
            }}
            onDeleted={name => { setServers(prev => prev.filter(s => s.name !== name)); setSelected(null); }}
            onCopy={copy => setSelected(copy)}
          />
        )}
      </div>
    </div>
  );
}

