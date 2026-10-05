import React, { useState } from "react";
import { APP_NAME } from "../lib/constants";

const field: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", padding: "9px 12px", borderRadius: 8,
  border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)", fontSize: 14,
};
const button: React.CSSProperties = {
  width: "100%", minHeight: 40, padding: "8px 12px", border: 0, borderRadius: 8,
  background: "var(--btn-bg)", color: "var(--btn-fg)", fontWeight: 600, cursor: "pointer", fontSize: 14,
};

function AuthPanel({ children, title, subtitle }: { children: React.ReactNode; title: string; subtitle: string }) {
  return <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "var(--bg)", color: "var(--fg)", padding: 20 }}>
    <section style={{ width: "100%", maxWidth: 390, border: "1px solid var(--border)", borderRadius: 10, background: "var(--card)", padding: 28, boxSizing: "border-box", boxShadow: "var(--shadow-soft)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 25 }}>
        <div style={{ width: 36, height: 36, borderRadius: 9, background: "var(--btn-bg)", display: "grid", placeItems: "center", color: "var(--btn-fg)", fontWeight: 900 }}>G</div>
        <strong>{APP_NAME}</strong>
      </div>
      <h1 style={{ fontSize: 21, margin: "0 0 6px" }}>{title}</h1>
      <p style={{ color: "var(--fg-muted)", fontSize: 13, margin: "0 0 22px", lineHeight: 1.5 }}>{subtitle}</p>
      {children}
    </section>
  </main>;
}

export function LoginPage({ onSignedIn, oidcConfigured }: { onSignedIn: () => void; oidcConfigured: boolean }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      const base = window.__GYRFALCON_BASE_PATH__ || "";
      const response = await fetch(`${base}/auth/local/login`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin", body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "Sign-in failed");
      }
      onSignedIn();
    } catch (e: any) { setError(e.message || "Unable to sign in"); }
    finally { setBusy(false); }
  }
  return <AuthPanel title="Sign in" subtitle="Use your Gyrfalcon account to continue.">
    <form onSubmit={submit} style={{ display: "grid", gap: 13 }}>
      <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>Username
        <input autoComplete="username" autoFocus value={username} onChange={e => setUsername(e.target.value)} style={field} />
      </label>
      <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>Password
        <input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} style={field} />
      </label>
      {error && <div role="alert" style={{ color: "var(--red)", fontSize: 13 }}>{error}</div>}
      <button style={{ ...button, opacity: busy ? 0.7 : 1 }} disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
    </form>
    {oidcConfigured && <a href={`${window.__GYRFALCON_BASE_PATH__ || ""}/auth/login`} style={{ display: "block", textAlign: "center", marginTop: 16, color: "var(--fg-muted)", fontSize: 13 }}>Sign in with SSO</a>}
  </AuthPanel>;
}

export function ChangePasswordPage({ onChanged }: { onChanged: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError("");
    if (next !== confirm) { setError("The new passwords do not match."); return; }
    setBusy(true);
    try {
      const base = window.__GYRFALCON_BASE_PATH__ || "";
      const response = await fetch(`${base}/auth/local/password`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Gyrfalcon-Session-Token": window.__GYRFALCON_SESSION_TOKEN__ || "" },
        credentials: "same-origin", body: JSON.stringify({ current_password: current, new_password: next }),
      });
      if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(payload.detail || "Password change failed"); }
      onChanged();
    } catch (e: any) { setError(e.message || "Password change failed"); }
    finally { setBusy(false); }
  }
  return <AuthPanel title="Change your password" subtitle="The default administrator password must be replaced before you continue. Any password is accepted.">
    <form onSubmit={submit} style={{ display: "grid", gap: 13 }}>
      <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>Current password<input type="password" autoComplete="current-password" value={current} onChange={e => setCurrent(e.target.value)} style={field} /></label>
      <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>New password<input type="password" autoComplete="new-password" value={next} onChange={e => setNext(e.target.value)} style={field} /></label>
      <label style={{ display: "grid", gap: 6, fontSize: 12, color: "var(--fg-muted)" }}>Confirm new password<input type="password" autoComplete="new-password" value={confirm} onChange={e => setConfirm(e.target.value)} style={field} /></label>
      {error && <div role="alert" style={{ color: "var(--red)", fontSize: 13 }}>{error}</div>}
      <button style={button} disabled={busy}>{busy ? "Saving…" : "Set password"}</button>
    </form>
  </AuthPanel>;
}
