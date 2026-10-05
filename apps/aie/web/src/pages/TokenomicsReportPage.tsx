import React, { useEffect, useMemo, useState } from "react";
import { fetchJSON } from "../lib/api";

type Dimension = "none" | "user" | "group" | "department" | "business_unit" | "model" | "provider";
type Grain = "day" | "week" | "month" | "quarter";

type ReportRow = {
  period: string;
  dimension: string;
  pivot: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
};

type ReportResponse = {
  rows: ReportRow[];
  totals: Omit<ReportRow, "period" | "dimension" | "pivot">;
  timezone: string;
};

const dimensions: { value: Dimension; label: string }[] = [
  { value: "none", label: "Overall" },
  { value: "user", label: "User" },
  { value: "group", label: "Group conversation" },
  { value: "department", label: "Department" },
  { value: "business_unit", label: "Business unit" },
  { value: "model", label: "Model" },
  { value: "provider", label: "Provider" },
];

const grains: { value: Grain; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "quarter", label: "Quarter" },
];

const card: React.CSSProperties = {
  background: "var(--card)",
  border: "1px solid var(--color-border)",
  borderRadius: 8,
  padding: "1rem",
};

const fieldLabel: React.CSSProperties = {
  display: "block",
  color: "var(--fg-muted)",
  fontSize: "0.78rem",
  marginBottom: "0.35rem",
};

const control: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: "var(--input-bg)",
  color: "var(--fg)",
  border: "1px solid var(--color-border)",
  borderRadius: 6,
  padding: "0.5rem 0.6rem",
  fontSize: "0.88rem",
};

function dateText(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function money(value: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: value && value < 0.01 ? 6 : 2,
    maximumFractionDigits: value && value < 0.01 ? 6 : 2,
  }).format(value || 0);
}

function number(value: number): string {
  return new Intl.NumberFormat().format(value || 0);
}

export function TokenomicsReportPage() {
  const today = new Date();
  const monthAgo = new Date();
  monthAgo.setDate(monthAgo.getDate() - 29);
  const [start, setStart] = useState(dateText(monthAgo));
  const [end, setEnd] = useState(dateText(today));
  const [grain, setGrain] = useState<Grain>("day");
  const [dimension, setDimension] = useState<Dimension>("model");
  const [pivot, setPivot] = useState<Dimension>("none");
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!start || !end) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ start, end, grain, dimension, pivot });
    setBusy(true);
    setError("");
    fetchJSON(`/api/tokenomics/report?${params.toString()}`, { signal: controller.signal })
      .then((data) => setReport(data))
      .catch((err) => {
        if (!controller.signal.aborted) setError(String(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [start, end, grain, dimension, pivot]);

  const periods = useMemo(() => {
    const sums = new Map<string, number>();
    for (const row of report?.rows || []) {
      sums.set(row.period, (sums.get(row.period) || 0) + Number(row.cost_usd || 0));
    }
    return [...sums.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [report]);

  const pivotColumns = useMemo(() => {
    const totals = new Map<string, number>();
    for (const row of report?.rows || []) {
      totals.set(row.pivot, (totals.get(row.pivot) || 0) + Number(row.cost_usd || 0));
    }
    const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]);
    if (pivot === "none") return ["All"];
    const top = ranked.slice(0, 10).map(([key]) => key);
    if (ranked.length > 10) top.push("Other");
    return top;
  }, [report, pivot]);

  const tableRows = useMemo(() => {
    const rows = new Map<string, {
      period: string;
      dimension: string;
      calls: number;
      input_tokens: number;
      output_tokens: number;
      costs: Map<string, number>;
    }>();
    for (const record of report?.rows || []) {
      const key = `${record.period}\u0000${record.dimension}`;
      const row = rows.get(key) || {
        period: record.period,
        dimension: record.dimension,
        calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        costs: new Map<string, number>(),
      };
      row.calls += Number(record.calls || 0);
      row.input_tokens += Number(record.input_tokens || 0);
      row.output_tokens += Number(record.output_tokens || 0);
      const pivotKey = pivotColumns.includes(record.pivot) ? record.pivot : "Other";
      row.costs.set(pivotKey, (row.costs.get(pivotKey) || 0) + Number(record.cost_usd || 0));
      rows.set(key, row);
    }
    return [...rows.values()].sort((a, b) =>
      a.period.localeCompare(b.period) || a.dimension.localeCompare(b.dimension)
    );
  }, [report, pivotColumns]);

  const maxCost = Math.max(0.000001, ...periods.map(([, amount]) => amount));
  const summaryCards = [
    { title: "Total cost", value: money(report?.totals.cost_usd || 0) },
    { title: "Model calls", value: number(report?.totals.calls || 0) },
    { title: "Input tokens", value: number(report?.totals.input_tokens || 0) },
    { title: "Output tokens", value: number(report?.totals.output_tokens || 0) },
  ];

  return (
    <div style={{ color: "var(--fg)", maxWidth: 1500 }}>
      <h2 style={{ margin: "0 0 0.25rem" }}>Tokenomics report</h2>
      <p style={{ color: "var(--fg-muted)", margin: "0 0 1rem", fontSize: "0.9rem" }}>
        Review recorded model usage and cost. Reports use UTC dates and the access scope of your account.
      </p>
      <p style={{ color: "var(--fg-muted)", margin: "-0.5rem 0 1rem", fontSize: "0.78rem" }}>
        Group reports use linked group conversations. Department and business-unit values come from organization membership metadata; missing values appear as Unassigned.
      </p>

      <section style={{ ...card, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(145px, 1fr))", gap: "0.75rem", marginBottom: "1rem" }}>
        <div><label style={fieldLabel} htmlFor="tokenomics-start">Start date</label><input id="tokenomics-start" type="date" value={start} max={end} onChange={(e) => setStart(e.target.value)} style={control} /></div>
        <div><label style={fieldLabel} htmlFor="tokenomics-end">End date</label><input id="tokenomics-end" type="date" value={end} min={start} onChange={(e) => setEnd(e.target.value)} style={control} /></div>
        <div><label style={fieldLabel} htmlFor="tokenomics-grain">Time bucket</label><select id="tokenomics-grain" value={grain} onChange={(e) => setGrain(e.target.value as Grain)} style={control}>{grains.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></div>
        <div><label style={fieldLabel} htmlFor="tokenomics-dimension">Break down by</label><select id="tokenomics-dimension" value={dimension} onChange={(e) => setDimension(e.target.value as Dimension)} style={control}>{dimensions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></div>
        <div><label style={fieldLabel} htmlFor="tokenomics-pivot">Pivot columns by</label><select id="tokenomics-pivot" value={pivot} onChange={(e) => setPivot(e.target.value as Dimension)} style={control}>{dimensions.map((item) => <option key={item.value} value={item.value}>{item.value === "none" ? "No pivot" : item.label}</option>)}</select></div>
      </section>

      {error && <div style={{ ...card, borderColor: "var(--red)", color: "var(--red)", marginBottom: "1rem" }}>{error}</div>}
      {busy && <div style={{ color: "var(--fg-muted)", marginBottom: "0.75rem", fontSize: "0.85rem" }}>Updating report…</div>}

      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "0.75rem", marginBottom: "1rem" }}>
        {summaryCards.map((item) => <div key={item.title} style={card}><div style={{ color: "var(--fg-muted)", fontSize: "0.8rem" }}>{item.title}</div><div style={{ fontSize: "1.35rem", fontWeight: 650, marginTop: "0.35rem" }}>{item.value}</div></div>)}
      </section>

      <section style={{ ...card, marginBottom: "1rem" }}>
        <h3 style={{ fontSize: "0.95rem", margin: "0 0 0.75rem" }}>Cost by {grain}</h3>
        {periods.length === 0 ? <p style={{ color: "var(--fg-muted)", margin: 0 }}>No recorded usage in this date range.</p> : (
          <div style={{ overflowX: "auto" }}>
            <svg role="img" aria-label={`Cost by ${grain}`} viewBox={`0 0 ${Math.max(720, periods.length * 34)} 220`} style={{ width: "100%", minWidth: 720, height: 220 }}>
              {periods.map(([period, amount], index) => {
                const width = Math.max(4, Math.min(24, 600 / periods.length));
                const gap = Math.max(6, 34 - width);
                const height = Math.max(2, (amount / maxCost) * 155);
                const x = 50 + index * (width + gap);
                const y = 175 - height;
                return <g key={period}><title>{`${period}: ${money(amount)}`}</title><rect x={x} y={y} width={width} height={height} rx="3" fill="var(--blue)" opacity="0.85" /><text x={x + width / 2} y="198" textAnchor="middle" fill="var(--fg-muted)" fontSize="9">{period.slice(-5)}</text></g>;
              })}
              <line x1="42" y1="176" x2={Math.max(720, periods.length * 34) - 12} y2="176" stroke="var(--color-border)" />
            </svg>
          </div>
        )}
      </section>

      <section style={{ ...card, padding: 0, overflow: "hidden" }}>
        <div style={{ padding: "1rem 1rem 0.5rem", display: "flex", justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap" }}>
          <h3 style={{ fontSize: "0.95rem", margin: 0 }}>Usage breakdown</h3>
          <span style={{ color: "var(--fg-muted)", fontSize: "0.78rem" }}>Pivot values show cost in USD</span>
        </div>
        {tableRows.length === 0 ? <p style={{ padding: "0 1rem 1rem", color: "var(--fg-muted)" }}>No usage rows match these filters.</p> : (
          <div style={{ overflow: "auto", maxHeight: 520 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem", minWidth: 680 }}>
              <thead><tr style={{ position: "sticky", top: 0, background: "var(--card)", borderBottom: "1px solid var(--color-border)" }}>
                <th style={{ padding: "0.6rem 0.75rem", textAlign: "left" }}>{grain[0].toUpperCase() + grain.slice(1)}</th>
                {dimension !== "none" && <th style={{ padding: "0.6rem 0.75rem", textAlign: "left" }}>{dimensions.find((d) => d.value === dimension)?.label}</th>}
                <th style={{ padding: "0.6rem 0.75rem", textAlign: "right" }}>Calls</th>
                <th style={{ padding: "0.6rem 0.75rem", textAlign: "right" }}>Input tokens</th>
                <th style={{ padding: "0.6rem 0.75rem", textAlign: "right" }}>Output tokens</th>
                {pivotColumns.map((value) => <th key={value} style={{ padding: "0.6rem 0.75rem", textAlign: "right", whiteSpace: "nowrap" }}>{pivot === "none" ? "Cost" : value}</th>)}
              </tr></thead>
              <tbody>{tableRows.map((row) => <tr key={`${row.period}-${row.dimension}`} style={{ borderBottom: "1px solid var(--color-border)" }}>
                <td style={{ padding: "0.55rem 0.75rem", whiteSpace: "nowrap" }}>{row.period}</td>
                {dimension !== "none" && <td style={{ padding: "0.55rem 0.75rem" }}>{row.dimension}</td>}
                <td style={{ padding: "0.55rem 0.75rem", textAlign: "right" }}>{number(row.calls)}</td>
                <td style={{ padding: "0.55rem 0.75rem", textAlign: "right" }}>{number(row.input_tokens)}</td>
                <td style={{ padding: "0.55rem 0.75rem", textAlign: "right" }}>{number(row.output_tokens)}</td>
                {pivotColumns.map((value) => <td key={value} style={{ padding: "0.55rem 0.75rem", textAlign: "right" }}>{money(row.costs.get(value) || 0)}</td>)}
              </tr>)}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
