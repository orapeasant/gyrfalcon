import React, { useState, useEffect, useMemo } from 'react';
import { api } from '../lib/api';

// ── Types ────────────────────────────────────────────────────────────────────

interface ProviderInfo {
  name: string;
  display_name: string;
  auth_type: string;
  authenticated: boolean;
  default_model: string;
}

interface ModelState {
  provider: string;
  model: string;
  providers: ProviderInfo[];
}

// ── Styles ───────────────────────────────────────────────────────────────────

const S = {
  page: {
    padding: '1.5rem',
    maxWidth: '960px',
    margin: '0 auto',
  } as React.CSSProperties,

  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: '1.5rem',
    flexWrap: 'wrap' as const,
    gap: '1rem',
  },

  headerLeft: {
    display: 'flex',
    alignItems: 'center',
    gap: '0.75rem',
  },

  title: {
    fontSize: '1.4rem',
    fontWeight: 700,
    color: 'var(--color-foreground)',
    margin: 0,
  },

  currentBadge: (changed: boolean): React.CSSProperties => ({
    display: 'inline-flex',
    alignItems: 'center',
    gap: '0.4rem',
    background: changed ? 'var(--sidebar-active)' : 'var(--color-midground)',
    border: `1px solid ${changed ? 'var(--border)' : 'var(--color-border)'}`,
    borderRadius: '6px',
    padding: '0.35rem 0.75rem',
    fontSize: '0.875rem',
  }),

  provider: {
    color: 'var(--fg)',
    fontWeight: 600,
  },

  separator: {
    color: 'var(--color-muted)',
  },

  model: {
    color: 'var(--color-foreground)',
    fontFamily: 'monospace',
  },

  saveBtn: (disabled: boolean): React.CSSProperties => ({
    background: disabled ? 'var(--btn-bg-disabled)' : 'var(--btn-bg)',
    color: disabled ? 'var(--btn-fg-disabled)' : 'var(--btn-fg)',
    border: 'none',
    borderRadius: '6px',
    padding: '0.5rem 1.25rem',
    fontWeight: 700,
    fontSize: '0.875rem',
    cursor: disabled ? 'not-allowed' : 'pointer',
    transition: 'opacity 0.15s',
  }),

  panels: {
    display: 'grid',
    gridTemplateColumns: '260px 1fr',
    gap: '1rem',
    alignItems: 'start',
  } as React.CSSProperties,

  panel: {
    background: 'var(--color-midground)',
    border: '1px solid var(--color-border)',
    borderRadius: '8px',
    overflow: 'hidden',
  },

  panelHeader: {
    padding: '0.75rem 1rem',
    borderBottom: '1px solid var(--color-border)',
    fontSize: '0.75rem',
    fontWeight: 700,
    textTransform: 'uppercase' as const,
    letterSpacing: '0.08em',
    color: 'var(--color-muted)',
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
  },

  providerCard: (selected: boolean, active: boolean): React.CSSProperties => ({
    display: 'flex',
    alignItems: 'center',
    padding: '0.75rem 1rem',
    cursor: 'pointer',
    borderLeft: selected
      ? '3px solid var(--fg-muted)'
      : '3px solid transparent',
    background: selected ? 'var(--sidebar-active)' : 'transparent',
    borderBottom: '1px solid var(--color-border)',
    transition: 'background 0.1s',
  }),

  providerName: (selected: boolean): React.CSSProperties => ({
    flex: 1,
    fontWeight: selected ? 700 : 400,
    color: selected ? 'var(--fg)' : 'var(--color-foreground)',
    fontSize: '0.9rem',
  }),

  authBadge: (ok: boolean): React.CSSProperties => ({
    fontSize: '0.7rem',
    padding: '0.1rem 0.4rem',
    borderRadius: '4px',
    background: ok ? 'rgba(52,211,153,0.15)' : 'rgba(248,113,113,0.15)',
    color: ok ? '#34d399' : '#f87171',
    fontWeight: 600,
  }),

  currentDot: {
    width: '6px',
    height: '6px',
    borderRadius: '50%',
    background: 'var(--color-primary)',
    marginLeft: '0.5rem',
    flexShrink: 0,
  } as React.CSSProperties,

  searchWrap: {
    padding: '0.75rem',
    borderBottom: '1px solid var(--color-border)',
  },

  searchInput: {
    width: '100%',
    background: 'var(--color-background)',
    border: '1px solid var(--color-border)',
    borderRadius: '6px',
    padding: '0.45rem 0.75rem',
    color: 'var(--color-foreground)',
    fontSize: '0.875rem',
    outline: 'none',
  } as React.CSSProperties,

  modelList: {
    maxHeight: '420px',
    overflowY: 'auto' as const,
  },

  modelRow: (selected: boolean, active: boolean): React.CSSProperties => ({
    display: 'flex',
    alignItems: 'center',
    padding: '0.6rem 1rem',
    cursor: 'pointer',
    borderLeft: selected
      ? '3px solid var(--fg-muted)'
      : '3px solid transparent',
    background: selected ? 'var(--sidebar-active)' : 'transparent',
    borderBottom: '1px solid rgba(42,42,58,0.5)',
    transition: 'background 0.1s',
  }),

  modelName: (selected: boolean): React.CSSProperties => ({
    flex: 1,
    fontFamily: 'monospace',
    fontSize: '0.875rem',
    fontWeight: selected ? 700 : 400,
    color: selected ? 'var(--fg)' : 'var(--color-foreground)',
  }),

  activePill: {
    fontSize: '0.7rem',
    padding: '0.1rem 0.5rem',
    borderRadius: '4px',
    background: 'var(--sidebar-active)',
    color: 'var(--fg)',
    fontWeight: 600,
    marginLeft: '0.5rem',
  } as React.CSSProperties,

  emptyState: {
    padding: '2rem',
    textAlign: 'center' as const,
    color: 'var(--color-muted)',
    fontSize: '0.875rem',
  },

  toast: (type: 'success' | 'error'): React.CSSProperties => ({
    position: 'fixed' as const,
    bottom: '1.5rem',
    right: '1.5rem',
    background: type === 'success' ? '#064e3b' : '#450a0a',
    border: `1px solid ${type === 'success' ? '#34d399' : '#f87171'}`,
    borderRadius: '8px',
    padding: '0.75rem 1.25rem',
    color: type === 'success' ? '#34d399' : '#f87171',
    fontWeight: 600,
    fontSize: '0.875rem',
    zIndex: 9999,
    boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
  }),

  stepLabel: {
    fontSize: '0.7rem',
    fontWeight: 700,
    textTransform: 'uppercase' as const,
    letterSpacing: '0.08em',
    color: 'var(--color-muted)',
    padding: '0 0 0.25rem 0',
  },
};

// ── Component ─────────────────────────────────────────────────────────────────

export function ModelsPage() {
  const [state, setState] = useState<ModelState | null>(null);
  const [selectedProvider, setSelectedProvider] = useState('');
  const [selectedModel, setSelectedModel] = useState('');
  const [providerModels, setProviderModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [pageLoading, setPageLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState('');
  const [toast, setToast] = useState<{ type: 'success' | 'error'; msg: string } | null>(null);

  // ── Load initial state ────────────────────────────────────────────────────

  useEffect(() => {
    loadState();
  }, []);

  async function loadState() {
    setPageLoading(true);
    try {
      const data: ModelState = await api.getModelState();
      setState(data);
      setSelectedProvider(data.provider);
      setSelectedModel(data.model);
      // Fetch models for current provider immediately
      await fetchModels(data.provider, data.model);
    } catch (e: any) {
      showToast('error', `Failed to load: ${e.message}`);
    }
    setPageLoading(false);
  }

  async function fetchModels(provider: string, keepModel?: string) {
    setModelsLoading(true);
    setSearch('');
    try {
      const data = await api.getProviderModels(provider);
      const models: string[] = data.models || [];
      setProviderModels(models);
      // Keep selection if available in new list, else default
      if (keepModel && models.includes(keepModel)) {
        setSelectedModel(keepModel);
      } else if (models.length > 0) {
        // Don't auto-change user's model selection — just leave it
      }
    } catch {
      setProviderModels([]);
    }
    setModelsLoading(false);
  }

  // ── Interactions ──────────────────────────────────────────────────────────

  function handleProviderClick(providerName: string) {
    if (providerName === selectedProvider) return;
    setSelectedProvider(providerName);
    // Find default model for this provider
    const prof = state?.providers.find(p => p.name === providerName);
    fetchModels(providerName, prof?.default_model);
  }

  function handleModelClick(model: string) {
    setSelectedModel(model);
  }

  async function handleSave() {
    if (!selectedProvider || !selectedModel) return;
    setSaving(true);
    try {
      await api.setModel(selectedModel, selectedProvider);
      setState(prev => prev ? { ...prev, provider: selectedProvider, model: selectedModel } : prev);
      showToast('success', `Saved: ${selectedProvider} › ${selectedModel}`);
    } catch (e: any) {
      showToast('error', `Save failed: ${e.message}`);
    }
    setSaving(false);
  }

  function showToast(type: 'success' | 'error', msg: string) {
    setToast({ type, msg });
    setTimeout(() => setToast(null), 3000);
  }

  // ── Derived ───────────────────────────────────────────────────────────────

  const hasChanges = state
    ? selectedProvider !== state.provider || selectedModel !== state.model
    : false;

  const filteredModels = useMemo(() => {
    if (!search.trim()) return providerModels;
    const q = search.toLowerCase();
    return providerModels.filter(m => m.toLowerCase().includes(q));
  }, [providerModels, search]);

  // ── Render ────────────────────────────────────────────────────────────────

  if (pageLoading) {
    return (
      <div style={S.page}>
        <div style={{ color: 'var(--color-muted)', padding: '2rem' }}>
          ⠋ Loading model configuration...
        </div>
      </div>
    );
  }

  return (
    <div style={S.page}>
      {/* Header */}
      <div style={S.header}>
        <div style={S.headerLeft}>
          <h1 style={S.title}>🤖 Model</h1>
          <div style={S.currentBadge(hasChanges)}>
            <span style={S.provider}>{selectedProvider || '—'}</span>
            <span style={S.separator}>›</span>
            <span style={S.model}>{selectedModel || '—'}</span>
            {hasChanges && (
              <span style={{ fontSize: '0.7rem', color: 'var(--fg)', fontWeight: 700 }}>
                ● unsaved
              </span>
            )}
          </div>
        </div>
        <button
          style={S.saveBtn(!hasChanges || saving)}
          disabled={!hasChanges || saving}
          onClick={handleSave}
        >
          {saving ? '⠋ Saving…' : hasChanges ? '💾 Save Changes' : '✓ Saved'}
        </button>
      </div>

      {/* Two-panel layout */}
      <div style={S.panels}>
        {/* Left: Providers */}
        <div>
          <div style={S.stepLabel}>① Provider</div>
          <div style={S.panel}>
            <div style={S.panelHeader}>
              <span>Provider</span>
              <span style={{ marginLeft: 'auto', fontWeight: 400, textTransform: 'none' }}>
                {state?.providers.length ?? 0} available
              </span>
            </div>
            {(state?.providers ?? []).map(p => {
              const isSelected = p.name === selectedProvider;
              const isActive = p.name === state?.provider;
              return (
                <div
                  key={p.name}
                  style={S.providerCard(isSelected, isActive)}
                  onClick={() => handleProviderClick(p.name)}
                >
                  <div style={S.providerName(isSelected)}>
                    {p.display_name}
                    {isActive && <span style={S.currentDot} title="Currently active" />}
                  </div>
                  <span style={S.authBadge(p.authenticated)}>
                    {p.authenticated ? '✓' : '✗'}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Right: Models */}
        <div>
          <div style={S.stepLabel}>② Model</div>
          <div style={S.panel}>
            <div style={S.panelHeader}>
              <span>Model</span>
              {!modelsLoading && (
                <span style={{ marginLeft: 'auto', fontWeight: 400, textTransform: 'none' }}>
                  {filteredModels.length} / {providerModels.length}
                </span>
              )}
            </div>

            <div style={S.searchWrap}>
              <input
                style={S.searchInput}
                placeholder="Search models…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>

            <div style={S.modelList}>
              {modelsLoading && (
                <div style={S.emptyState}>⠋ Loading models…</div>
              )}
              {!modelsLoading && filteredModels.length === 0 && (
                <div style={S.emptyState}>
                  {search ? 'No models match your search.' : 'No models available for this provider.'}
                </div>
              )}
              {!modelsLoading && filteredModels.map(m => {
                const isSelected = m === selectedModel;
                const isActive = m === state?.model && selectedProvider === state?.provider;
                return (
                  <div
                    key={m}
                    style={S.modelRow(isSelected, isActive)}
                    onClick={() => handleModelClick(m)}
                  >
                    <span style={S.modelName(isSelected)}>{m}</span>
                    {isActive && <span style={S.activePill}>active</span>}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div style={S.toast(toast.type)}>{toast.msg}</div>
      )}
    </div>
  );
}
