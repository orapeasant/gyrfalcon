import React, { useState, useEffect } from 'react';
import { fetchJSON } from '../lib/api';
import { CollectionViewToggle, collectionCardStyle, collectionStyle, useCollectionView } from '../components/CollectionViewToggle';

interface PluginInfo {
  name: string;
  version: string;
  description: string;
  enabled: boolean;
  source: string;
  hooks: string[];
}

export function PluginsPage() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useCollectionView('plugins');

  useEffect(() => { loadPlugins(); }, []);

  async function loadPlugins() {
    setLoading(true);
    try {
      const data = await fetchJSON('/api/plugins');
      setPlugins(data.plugins || []);
    } catch { setPlugins([]); }
    setLoading(false);
  }

  async function togglePlugin(name: string, enabled: boolean) {
    await fetchJSON(`/api/plugins/${name}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' });
    loadPlugins();
  }

  if (loading) return <div className="p-4">Loading plugins...</div>;

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}><h1 className="text-2xl font-bold">Plugins</h1><CollectionViewToggle view={view} onChange={setView} label="Plugin view" /></div>

      <div style={collectionStyle(view)}>
        {plugins.length === 0 && <p className="text-gray-500">No plugins installed.</p>}
        {plugins.map(plugin => (
          <div key={plugin.name} className="border rounded p-4" style={collectionCardStyle(view)}>
            <div className="flex items-center justify-between">
              <div>
                <h3 className="font-semibold">{plugin.name} <span className="text-xs text-gray-400">v{plugin.version}</span></h3>
                <p className="text-sm text-gray-600">{plugin.description}</p>
                <div className="flex gap-2 mt-1">
                  <span className="text-xs bg-gray-100 rounded px-1">{plugin.source}</span>
                  {plugin.hooks.map(h => (
                    <span key={h} className="text-xs bg-purple-100 text-purple-700 rounded px-1">{h}</span>
                  ))}
                </div>
              </div>
              <button
                className={`px-3 py-1 rounded text-sm ${plugin.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}
                onClick={() => togglePlugin(plugin.name, !plugin.enabled)}
              >
                {plugin.enabled ? 'Enabled' : 'Disabled'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
