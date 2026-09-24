/**
 * Tokenomics — estimate what a prompt or document costs, per model.
 *
 * Spec: docs/spec/gyrfalcon/19-tokenomics.md §19.5.5.
 *
 * Two things this page is careful about:
 *  - every token figure shows how it was counted (exact / api / unavailable),
 *    and a model with no tokenizer shows no number rather than a guess;
 *  - caching is presented as a curve, not a single cost, because writing to
 *    cache costs more than not caching and only pays off on reuse.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON } from "../lib/api";

type ModelRow = {
  model: string;
  provider: string;
  billing: string;
  input_per_1k: number;
  output_per_1k: number;
  context_window: number | null;
  countable: boolean;
  method: string;
  tokenizer: string;
  detail: string;
};

type ReusePoint = {
  calls: number;
  uncached_cost: number;
  cached_cost: number;
  saving: number;
};

type Estimate = {
  model: string;
  provider: string;
  billing: string;
  available: boolean;
  input_tokens: number;
  method: string;
  tokenizer: string;
  detail: string;
  output_tokens: number;
  input_cost: number;
  output_cost: number;
  cache_write_cost_5m: number;
  cache_write_cost_1h: number;
  cache_read_cost: number;
  total_cost: number;
  break_even_calls: number | null;
  reuses: ReusePoint[];
  context_window: number | null;
  fits_context: boolean | null;
  context_used_pct: number | null;
  rates_source: string;
};

type Result = {
  input_chars: number;
  output_tokens: number;
  overhead_tokens: number;
  include_agent_prompt: boolean;
  warnings: string[];
  models: Estimate[];
};

const DEFAULT_MODELS = ["gpt-4o", "claude-sonnet-4-5", "gemini-2.5-pro"];

const card: React.CSSProperties = {
  background: "var(--card)",
  border: "1px solid var(--color-border)",
  borderRadius: 8,
  padding: "1rem",
};

const label: React.CSSProperties = {
  display: "block",
  fontSize: "0.8rem",
  color: "var(--fg-muted)",
  marginBottom: "0.35rem",
};

const input: React.CSSProperties = {
  background: "var(--input-bg)",
  color: "var(--fg)",
  border: "1px solid var(--color-border)",
  borderRadius: 6,
  padding: "0.45rem 0.6rem",
  width: "100%",
  fontSize: "0.9rem",
};

function money(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(6)}`;
  return `$${value.toFixed(4)}`;
}

function MethodBadge({ method, detail }: { method: string; detail: string }) {
  const palette: Record<string, [string, string]> = {
    exact: ["var(--green)", "exact"],
    api: ["var(--blue)", "exact (API)"],
    unavailable: ["var(--fg-subtle)", "unavailable"],
  };
  const [color, text] = palette[method] || ["var(--fg-subtle)", method];
  return (
    <span
      title={detail || text}
      style={{
        color,
        border: `1px solid ${color}`,
        borderRadius: 4,
        padding: "0.05rem 0.35rem",
        fontSize: "0.7rem",
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

export function TokenomicsPage() {
  const [text, setText] = useState("");
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [uploadInfo, setUploadInfo] = useState<any>(null);
  const [catalog, setCatalog] = useState<ModelRow[]>([]);
  const [catalogMeta, setCatalogMeta] = useState<any>(null);
  const [selected, setSelected] = useState<string[]>(DEFAULT_MODELS);
  const [search, setSearch] = useState("");
  const [outputTokens, setOutputTokens] = useState(500);
  const [includePrompt, setIncludePrompt] = useState(true);
  const [cacheTtl, setCacheTtl] = useState("5m");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    fetchJSON("/api/tokenomics/models?limit=400")
      .then((r) => {
        setCatalog(r.models || []);
        setCatalogMeta(r.catalog || null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const visible = useMemo(() => {
    const needle = search.toLowerCase().trim();
    const rows = needle
      ? catalog.filter((m) => m.model.toLowerCase().includes(needle))
      : catalog;
    return rows.slice(0, 60);
  }, [catalog, search]);

  function toggle(model: string) {
    setSelected((prev) =>
      prev.includes(model) ? prev.filter((m) => m !== model) : [...prev, model],
    );
  }

  async function upload(file: File) {
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(
        `${window.__GYRFALCON_BASE_PATH__ || ""}/api/tokenomics/upload`,
        {
          method: "POST",
          headers: {
            "X-Gyrfalcon-Session-Token": window.__GYRFALCON_SESSION_TOKEN__ || "",
          },
          body: form,
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const info = await response.json();
      setUploadId(info.id);
      setUploadInfo(info);
      setText("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function run() {
    if (!selected.length) {
      setError("Select at least one model.");
      return;
    }
    if (!text.trim() && !uploadId) {
      setError("Paste some text or upload a file.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const body: any = {
        models: selected,
        output_tokens: outputTokens,
        include_agent_prompt: includePrompt,
        cache_ttl: cacheTtl,
      };
      if (uploadId) body.upload_id = uploadId;
      else body.text = text;
      setResult(await fetchJSON("/api/tokenomics/estimate", {
        method: "POST",
        body: JSON.stringify(body),
      }));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function refreshPrices() {
    setBusy(true);
    try {
      const r = await fetchJSON("/api/pricing/refresh", { method: "POST" });
      setCatalogMeta(r.catalog);
      if (!r.ok) setError(`Price refresh failed: ${r.error}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const priced = (result?.models || []).filter((m) => m.available);
  const maxCost = Math.max(0.000001, ...priced.map((m) => m.total_cost));

  return (
    <div style={{ color: "var(--fg)", maxWidth: 1200 }}>
      <h2 style={{ marginBottom: "0.25rem" }}>Tokenomics</h2>
      <p style={{ color: "var(--fg-muted)", marginTop: 0, fontSize: "0.9rem" }}>
        Estimate token count and cost before you spend anything. No model is
        called — counting is local, or via Anthropic's free token-counting API.
      </p>

      {error && (
        <div style={{ ...card, borderColor: "var(--red)", color: "var(--red)", marginBottom: "1rem" }}>
          {error}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,360px)", gap: "1rem" }}>
        {/* ── Input ─────────────────────────────────────────────── */}
        <div style={card}>
          <label style={label}>Text</label>
          <textarea
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setUploadId(null);
              setUploadInfo(null);
            }}
            placeholder="Paste a prompt, a document, or some code…"
            style={{ ...input, minHeight: 200, fontFamily: "inherit", resize: "vertical" }}
          />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "0.5rem", gap: "0.5rem", flexWrap: "wrap" }}>
            <span style={{ color: "var(--fg-subtle)", fontSize: "0.8rem" }}>
              {uploadInfo
                ? `${uploadInfo.filename} — ${uploadInfo.chars.toLocaleString()} chars${uploadInfo.pages ? `, ${uploadInfo.pages} pages` : ""}`
                : `${text.length.toLocaleString()} characters`}
            </span>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <input
                ref={fileRef}
                type="file"
                style={{ display: "none" }}
                onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
              />
              <button onClick={() => fileRef.current?.click()} disabled={busy}
                      style={{ ...input, width: "auto", cursor: "pointer" }}>
                Upload file
              </button>
              <button onClick={run} disabled={busy}
                      style={{ ...input, width: "auto", cursor: "pointer", background: "var(--primary)", color: "var(--btn-fg)", borderColor: "var(--primary)" }}>
                {busy ? "Working…" : "Estimate"}
              </button>
            </div>
          </div>

          {uploadInfo?.warnings?.length > 0 && (
            <ul style={{ color: "var(--red)", fontSize: "0.8rem", marginBottom: 0 }}>
              {uploadInfo.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}
            </ul>
          )}
        </div>

        {/* ── Options ───────────────────────────────────────────── */}
        <div style={card}>
          <label style={label}>Assumed response length (tokens)</label>
          <input type="number" min={0} step={100} value={outputTokens} style={input}
                 onChange={(e) => setOutputTokens(Math.max(0, Number(e.target.value) || 0))} />

          <label style={{ ...label, marginTop: "0.75rem" }}>Cache TTL</label>
          <select value={cacheTtl} onChange={(e) => setCacheTtl(e.target.value)} style={input}>
            <option value="5m">5 minutes</option>
            <option value="1h">1 hour</option>
          </select>

          <label style={{ display: "flex", gap: "0.5rem", alignItems: "flex-start", marginTop: "0.9rem", cursor: "pointer" }}>
            <input type="checkbox" checked={includePrompt}
                   onChange={(e) => setIncludePrompt(e.target.checked)} />
            <span style={{ fontSize: "0.85rem" }}>
              Include the agent's system prompt and tool schemas
              <span style={{ display: "block", color: "var(--fg-subtle)", fontSize: "0.75rem" }}>
                What a real request actually sends. Usually thousands of tokens
                before your text begins.
              </span>
            </span>
          </label>

          {catalogMeta && (
            <div style={{ marginTop: "0.9rem", paddingTop: "0.9rem", borderTop: "1px solid var(--color-border)", fontSize: "0.75rem", color: "var(--fg-subtle)" }}>
              {catalogMeta.total} models priced
              {catalogMeta.overlay_applied && " · local overrides active"}
              <button onClick={refreshPrices} disabled={busy}
                      style={{ ...input, width: "auto", padding: "0.2rem 0.5rem", marginTop: "0.5rem", cursor: "pointer", fontSize: "0.75rem" }}>
                Refresh prices
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── Model picker ────────────────────────────────────────── */}
      <div style={{ ...card, marginTop: "1rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "1rem", marginBottom: "0.6rem" }}>
          <strong style={{ fontSize: "0.9rem" }}>Models ({selected.length} selected)</strong>
          <input value={search} onChange={(e) => setSearch(e.target.value)}
                 placeholder="Filter models…" style={{ ...input, maxWidth: 260 }} />
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", maxHeight: 190, overflowY: "auto" }}>
          {visible.map((m) => {
            const on = selected.includes(m.model);
            return (
              <button key={m.model} onClick={() => toggle(m.model)}
                      title={m.countable ? `${m.method} · ${m.tokenizer}` : m.detail}
                      style={{
                        border: `1px solid ${on ? "var(--primary)" : "var(--color-border)"}`,
                        background: on ? "var(--primary-dim)" : "transparent",
                        color: m.countable ? "var(--fg)" : "var(--fg-subtle)",
                        borderRadius: 999, padding: "0.2rem 0.6rem",
                        fontSize: "0.78rem", cursor: "pointer",
                      }}>
                {m.model}
                {!m.countable && " ⚠"}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Results ─────────────────────────────────────────────── */}
      {result && (
        <div style={{ ...card, marginTop: "1rem" }}>
          {result.include_agent_prompt && result.overhead_tokens > 0 && (
            <p style={{ color: "var(--fg-muted)", fontSize: "0.82rem", marginTop: 0 }}>
              Includes <strong>{result.overhead_tokens.toLocaleString()}</strong> tokens
              of system prompt and tool schemas that every request carries.
            </p>
          )}
          {result.warnings.map((w, i) => (
            <p key={i} style={{ color: "var(--fg-subtle)", fontSize: "0.8rem", margin: "0.2rem 0" }}>⚠ {w}</p>
          ))}

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem", marginTop: "0.6rem" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--color-border)", color: "var(--fg-muted)" }}>
                  <th style={{ textAlign: "left", padding: "0.45rem" }}>Model</th>
                  <th style={{ textAlign: "left", padding: "0.45rem" }}>Count</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Tokens</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Input</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Output</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Cache write</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Cache read</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Total</th>
                  <th style={{ textAlign: "right", padding: "0.45rem" }}>Context</th>
                </tr>
              </thead>
              <tbody>
                {result.models.map((m) => (
                  <tr key={m.model} style={{ borderBottom: "1px solid var(--color-border)" }}>
                    <td style={{ padding: "0.45rem" }}>
                      {m.model}
                      {m.billing === "credits" && (
                        <span style={{ color: "var(--fg-subtle)", fontSize: "0.72rem" }}> · credits</span>
                      )}
                    </td>
                    <td style={{ padding: "0.45rem" }}>
                      <MethodBadge method={m.method} detail={m.detail} />
                    </td>
                    {m.available ? (
                      <>
                        <td style={{ padding: "0.45rem", textAlign: "right" }}>{m.input_tokens.toLocaleString()}</td>
                        <td style={{ padding: "0.45rem", textAlign: "right" }}>{money(m.input_cost)}</td>
                        <td style={{ padding: "0.45rem", textAlign: "right" }}>{money(m.output_cost)}</td>
                        <td style={{ padding: "0.45rem", textAlign: "right" }}>
                          {m.cache_write_cost_5m > 0 ? money(m.cache_write_cost_5m)
                            : <span style={{ color: "var(--fg-subtle)" }}>free</span>}
                        </td>
                        <td style={{ padding: "0.45rem", textAlign: "right" }}>{money(m.cache_read_cost)}</td>
                        <td style={{ padding: "0.45rem", textAlign: "right", fontWeight: 600 }}>{money(m.total_cost)}</td>
                        <td style={{ padding: "0.45rem", textAlign: "right", color: m.fits_context === false ? "var(--red)" : "var(--fg-subtle)" }}>
                          {m.context_used_pct != null ? `${m.context_used_pct}%` : "—"}
                        </td>
                      </>
                    ) : (
                      <td colSpan={7} style={{ padding: "0.45rem", color: "var(--fg-subtle)" }}>
                        {m.detail}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Relative cost bars */}
          {priced.length > 1 && (
            <div style={{ marginTop: "1rem" }}>
              <strong style={{ fontSize: "0.85rem" }}>Relative cost</strong>
              {priced.slice().sort((a, b) => a.total_cost - b.total_cost).map((m) => (
                <div key={m.model} style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginTop: "0.35rem" }}>
                  <span style={{ width: 190, fontSize: "0.78rem", color: "var(--fg-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {m.model}
                  </span>
                  <div style={{ flex: 1, background: "var(--color-midground)", borderRadius: 4, height: 16 }}>
                    <div style={{ width: `${Math.max(2, (m.total_cost / maxCost) * 100)}%`, background: "var(--primary)", height: "100%", borderRadius: 4 }} />
                  </div>
                  <span style={{ width: 90, textAlign: "right", fontSize: "0.78rem" }}>{money(m.total_cost)}</span>
                </div>
              ))}
            </div>
          )}

          {/* Cache break-even */}
          {priced.length > 0 && priced[0].reuses.length > 0 && (
            <div style={{ marginTop: "1.25rem" }}>
              <strong style={{ fontSize: "0.85rem" }}>Does caching pay off?</strong>
              <p style={{ color: "var(--fg-subtle)", fontSize: "0.78rem", margin: "0.2rem 0 0.5rem" }}>
                Cost of sending this same prefix N times, cached versus not.
                Writing to cache costs more up front, so it only wins on reuse.
              </p>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid var(--color-border)", color: "var(--fg-muted)" }}>
                      <th style={{ textAlign: "left", padding: "0.4rem" }}>Model</th>
                      <th style={{ textAlign: "right", padding: "0.4rem" }}>Breaks even at</th>
                      {priced[0].reuses.map((p) => (
                        <th key={p.calls} style={{ textAlign: "right", padding: "0.4rem" }}>×{p.calls}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {priced.map((m) => (
                      <tr key={m.model} style={{ borderBottom: "1px solid var(--color-border)" }}>
                        <td style={{ padding: "0.4rem" }}>{m.model}</td>
                        <td style={{ padding: "0.4rem", textAlign: "right" }}>
                          {m.break_even_calls == null ? "—"
                            : m.break_even_calls <= 1 ? "always cheaper"
                            : `${m.break_even_calls.toFixed(2)} calls`}
                        </td>
                        {m.reuses.map((p) => (
                          <td key={p.calls} style={{ padding: "0.4rem", textAlign: "right" }}>
                            <div>{money(p.cached_cost)}</div>
                            <div style={{ fontSize: "0.7rem", color: p.saving > 0 ? "var(--green)" : "var(--fg-subtle)" }}>
                              {p.saving > 0 ? `saves ${money(p.saving)}` : "no saving"}
                            </div>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
