import React, { useState, useEffect } from "react";
import { api } from "../lib/api";

export function AnalyticsPage() {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    api.getAnalytics(30).then((res) => {
      const sorted = (res.daily || []).slice().sort((a: any, b: any) =>
        new Date(b.day).getTime() - new Date(a.day).getTime()
      );
      setData(sorted);
    }).catch(() => {});
  }, []);

  return (
    <div>
      <h2 style={{ color: "var(--fg)" }}>Analytics</h2>
      {data.length === 0 ? (
        <p style={{ color: "var(--color-muted)" }}>No usage data yet.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
              <th style={{ textAlign: "left", padding: "0.5rem" }}>Day</th>
              <th style={{ textAlign: "right", padding: "0.5rem" }}>Sessions</th>
              <th style={{ textAlign: "right", padding: "0.5rem" }}>Input Tokens</th>
              <th style={{ textAlign: "right", padding: "0.5rem" }}>Output Tokens</th>
              <th style={{ textAlign: "right", padding: "0.5rem" }}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row: any, i: number) => (
              <tr key={i} style={{ borderBottom: "1px solid var(--color-border)" }}>
                <td style={{ padding: "0.5rem" }}>{row.day}</td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>{row.sessions}</td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>{row.input_tokens?.toLocaleString()}</td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>{row.output_tokens?.toLocaleString()}</td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>${(row.cost || 0).toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
