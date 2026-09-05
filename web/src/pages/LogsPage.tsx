import React, { useState, useEffect } from 'react';
import { fetchJSON } from '../lib/api';

interface LogEntry {
  timestamp: string;
  level: string;
  module: string;
  message: string;
}

export function LogsPage() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState('INFO');
  const [autoRefresh, setAutoRefresh] = useState(false);

  useEffect(() => { loadLogs(); }, [level]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(loadLogs, 3000);
    return () => clearInterval(interval);
  }, [autoRefresh, level]);

  async function loadLogs() {
    try {
      const data = await fetchJSON(`/api/logs?level=${level}&limit=200`);
      setLogs(data.logs || []);
    } catch { setLogs([]); }
  }

  const levelColors: Record<string, string> = {
    DEBUG: 'text-gray-400',
    INFO: 'text-blue-600',
    WARNING: 'text-yellow-600',
    ERROR: 'text-red-600',
    CRITICAL: 'text-red-800 font-bold',
  };

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">Logs</h1>
        <div className="flex gap-3 items-center">
          <select
            className="border rounded px-2 py-1"
            value={level}
            onChange={e => setLevel(e.target.value)}
          >
            <option>DEBUG</option>
            <option>INFO</option>
            <option>WARNING</option>
            <option>ERROR</option>
          </select>
          <label className="flex items-center gap-1 text-sm">
            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
            Auto-refresh
          </label>
          <button className="text-sm bg-gray-200 px-3 py-1 rounded" onClick={loadLogs}>
            Refresh
          </button>
        </div>
      </div>

      <div className="bg-gray-900 text-gray-100 rounded p-4 font-mono text-xs overflow-auto max-h-[70vh]">
        {logs.length === 0 && <p className="text-gray-500">No logs found.</p>}
        {logs.map((entry, i) => (
          <div key={i} className="flex gap-2 hover:bg-gray-800 py-0.5">
            <span className="text-gray-500 w-44 shrink-0">{entry.timestamp}</span>
            <span className={`w-16 shrink-0 ${levelColors[entry.level] || ''}`}>{entry.level}</span>
            <span className="text-gray-400 w-24 shrink-0 truncate">{entry.module}</span>
            <span className="break-all">{entry.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
