import React, { useState, useEffect, useCallback } from 'react';
import { fetchJSON } from '../lib/api';
import { Plus, Trash2, FileText, Code, BookOpen, X, Upload } from 'lucide-react';

interface SkillFile { name: string; size: number; path: string; }

interface Skill {
  name: string;
  description: string;
  version: string;
  author: string;
  body: string;
  path: string;
  type: string;       // "user" | "legacy"
  folder: string;
  references: SkillFile[];
  scripts:    SkillFile[];
}

type Tab = 'skill' | 'references' | 'scripts';

const NEW_SKILL_TEMPLATE = `---
name: my-skill
description: Describe what this skill does
version: 1.0.0
author: user
---

# My Skill

Add skill instructions here.
`;

// ── helpers ──────────────────────────────────────────────────────────────────

async function apiSkill(path: string, opts: RequestInit = {}) {
  return fetchJSON(`/api/skills${path}`, opts);
}

function fmtSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

// ── Main page ─────────────────────────────────────────────────────────────────

export function SkillsPage() {
  const [skills, setSkills]           = useState<Skill[]>([]);
  const [loading, setLoading]         = useState(true);
  const [selected, setSelected]       = useState<Skill | null>(null);
  const [creating, setCreating]       = useState(false);
  const [newSkillName, setNewSkillName] = useState('');
  const [activeTab, setActiveTab]     = useState<Tab>('skill');

  // SKILL.md editor
  const [editorContent, setEditorContent] = useState('');
  const [dirty, setDirty]             = useState(false);
  const [saving, setSaving]           = useState(false);
  const [saveMsg, setSaveMsg]         = useState<{ text: string; ok: boolean } | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  // Sub-file editor (references / scripts)
  const [subFiles, setSubFiles]           = useState<SkillFile[]>([]);
  const [selectedFile, setSelectedFile]   = useState<SkillFile | null>(null);
  const [fileContent, setFileContent]     = useState('');
  const [fileDirty, setFileDirty]         = useState(false);
  const [fileSaving, setFileSaving]       = useState(false);
  const [newFileName, setNewFileName]     = useState('');
  const [fileMsg, setFileMsg]             = useState<{ text: string; ok: boolean } | null>(null);
  const [deletingFile, setDeletingFile]   = useState<string | null>(null);

  useEffect(() => { loadSkills(); }, []);

  async function loadSkills(keepSelected?: string) {
    setLoading(true);
    try {
      const data = await apiSkill('');
      const list: Skill[] = data.skills || [];
      setSkills(list);
      if (keepSelected) {
        const refreshed = list.find(s => s.name === keepSelected || s.folder === keepSelected);
        if (refreshed) {
          setSelected(refreshed);
          setEditorContent(refreshed.body || '');
          setDirty(false);
        }
      }
    } catch { setSkills([]); }
    setLoading(false);
  }

  function selectSkill(skill: Skill) {
    if (dirty && !confirm('Discard unsaved changes?')) return;
    setSelected(skill);
    setEditorContent(skill.body || '');
    setDirty(false);
    setSaveMsg(null);
    setCreating(false);
    setActiveTab('skill');
    setSelectedFile(null);
    setFileContent('');
    setFileDirty(false);
    setFileMsg(null);
    setSubFiles(skill.references || []);
  }

  // ── Tab switching ──────────────────────────────────────────────────────────

  async function switchTab(tab: Tab) {
    setActiveTab(tab);
    setSaveMsg(null);
    setFileMsg(null);
    setSelectedFile(null);
    setFileContent('');
    setFileDirty(false);
    if (!selected || creating) return;
    if (tab === 'references' || tab === 'scripts') {
      await loadSubFiles(selected.name, tab);
    }
  }

  async function loadSubFiles(skillName: string, tab: Tab) {
    try {
      const data = await apiSkill(`/${encodeURIComponent(skillName)}/files/${tab}`);
      setSubFiles(data.files || []);
    } catch { setSubFiles([]); }
  }

  // ── SKILL.md editor ────────────────────────────────────────────────────────

  async function saveSkill() {
    if (!selected) return;
    setSaving(true); setSaveMsg(null);
    try {
      await apiSkill(`/${encodeURIComponent(selected.name)}`, {
        method: 'PUT',
        body: JSON.stringify({ content: editorContent }),
      });
      setSaveMsg({ text: 'Saved', ok: true });
      setDirty(false);
      await loadSkills(selected.name);
    } catch (e: any) {
      setSaveMsg({ text: e.message || 'Save failed', ok: false });
    }
    setSaving(false);
  }

  async function deleteSkill(name: string) {
    try {
      await apiSkill(`/${encodeURIComponent(name)}`, { method: 'DELETE' });
      setSelected(null); setEditorContent(''); setDirty(false);
      setDeleteConfirm(null);
      await loadSkills();
    } catch (e: any) {
      setSaveMsg({ text: e.message || 'Delete failed', ok: false });
    }
  }

  function startCreate() {
    if (dirty && !confirm('Discard unsaved changes?')) return;
    setCreating(true); setSelected(null); setNewSkillName('');
    setEditorContent(NEW_SKILL_TEMPLATE); setDirty(false); setSaveMsg(null);
    setActiveTab('skill');
  }

  async function createSkill() {
    const name = newSkillName.trim();
    if (!name) return;
    setSaving(true); setSaveMsg(null);
    try {
      await apiSkill('', {
        method: 'POST',
        body: JSON.stringify({ name, content: editorContent, description: '' }),
      });
      setSaveMsg({ text: `"${name}" created`, ok: true });
      setCreating(false); setDirty(false);
      await loadSkills(name);
    } catch (e: any) {
      setSaveMsg({ text: e.message || 'Create failed', ok: false });
    }
    setSaving(false);
  }

  // ── Sub-file editor ────────────────────────────────────────────────────────

  async function openFile(file: SkillFile) {
    if (fileDirty && !confirm('Discard unsaved file changes?')) return;
    setSelectedFile(file);
    setFileMsg(null);
    try {
      const data = await apiSkill(
        `/${encodeURIComponent(selected!.name)}/files/${activeTab}/${encodeURIComponent(file.name)}`
      );
      setFileContent(data.content || '');
      setFileDirty(false);
    } catch { setFileContent(''); }
  }

  async function saveFile() {
    if (!selected || !selectedFile) return;
    setFileSaving(true); setFileMsg(null);
    try {
      await apiSkill(
        `/${encodeURIComponent(selected.name)}/files/${activeTab}/${encodeURIComponent(selectedFile.name)}`,
        { method: 'PUT', body: JSON.stringify({ content: fileContent }) }
      );
      setFileMsg({ text: 'Saved', ok: true });
      setFileDirty(false);
      await loadSubFiles(selected.name, activeTab);
    } catch (e: any) {
      setFileMsg({ text: e.message || 'Save failed', ok: false });
    }
    setFileSaving(false);
  }

  async function createFile() {
    const fn = newFileName.trim();
    if (!fn || !selected) return;
    setFileSaving(true); setFileMsg(null);
    try {
      await apiSkill(
        `/${encodeURIComponent(selected.name)}/files/${activeTab}/${encodeURIComponent(fn)}`,
        { method: 'PUT', body: JSON.stringify({ content: '' }) }
      );
      setNewFileName('');
      await loadSubFiles(selected.name, activeTab);
      // Auto-open the new file
      const created: SkillFile = { name: fn, size: 0, path: '' };
      setSelectedFile(created); setFileContent(''); setFileDirty(false);
      setFileMsg({ text: `"${fn}" created`, ok: true });
    } catch (e: any) {
      setFileMsg({ text: e.message || 'Create failed', ok: false });
    }
    setFileSaving(false);
  }

  async function deleteFile(filename: string) {
    if (!selected) return;
    try {
      await apiSkill(
        `/${encodeURIComponent(selected.name)}/files/${activeTab}/${encodeURIComponent(filename)}`,
        { method: 'DELETE' }
      );
      if (selectedFile?.name === filename) { setSelectedFile(null); setFileContent(''); }
      setDeletingFile(null);
      await loadSubFiles(selected.name, activeTab);
    } catch (e: any) {
      setFileMsg({ text: e.message || 'Delete failed', ok: false });
    }
  }

  // ── Styles ─────────────────────────────────────────────────────────────────

  const hasEditor = selected || creating;

  const tabBtn = (t: Tab): React.CSSProperties => ({
    padding: '5px 14px', fontSize: '0.82rem', fontWeight: activeTab === t ? 600 : 400,
    border: 'none', borderRadius: '5px', cursor: 'pointer',
    background: activeTab === t ? 'var(--sidebar-active)' : 'transparent',
    color: activeTab === t ? 'var(--fg)' : 'var(--color-muted)',
    display: 'flex', alignItems: 'center', gap: '5px',
  });

  const fileRow = (active: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: '6px',
    padding: '6px 10px', cursor: 'pointer', borderRadius: '5px',
    background: active ? 'var(--sidebar-active)' : 'transparent',
    borderLeft: active ? '2px solid var(--fg-muted)' : '2px solid transparent',
    fontSize: '0.83rem',
  });

  return (
    <div style={{ display: 'flex', height: '100%', gap: 0, overflow: 'hidden' }}>

      {/* ── LEFT: skill list ── */}
      <div style={{
        width: '240px', minWidth: '180px', flexShrink: 0,
        borderRight: '1px solid var(--color-border)',
        display: 'flex', flexDirection: 'column',
        background: 'var(--color-midground)',
      }}>
        <div style={{
          padding: '0.65rem 1rem', borderBottom: '1px solid var(--color-border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span style={{ fontWeight: 600, fontSize: '0.88rem' }}>Skills</span>
          <button onClick={startCreate} title="New skill" style={{
            background: 'var(--btn-bg)', color: 'var(--btn-fg)',
            border: 'none', borderRadius: '4px', padding: '2px 8px',
            cursor: 'pointer', fontSize: '1rem', lineHeight: '1.4',
          }}>+</button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto' }}>
          {loading && <div style={{ padding: '1rem', color: 'var(--color-muted)', fontSize: '0.85rem' }}>Loading…</div>}
          {!loading && skills.length === 0 && (
            <div style={{ padding: '1rem', color: 'var(--color-muted)', fontSize: '0.85rem' }}>
              No skills found.
            </div>
          )}
          {skills.map(skill => {
            const isActive = selected?.name === skill.name && !creating;
            return (
              <div key={skill.name} onClick={() => selectSkill(skill)} style={{
                padding: '0.6rem 1rem', cursor: 'pointer',
                borderLeft: isActive ? '3px solid var(--fg-muted)' : '3px solid transparent',
                background: isActive ? 'var(--color-background)' : 'transparent',
                borderBottom: '1px solid var(--color-border)', overflow: 'hidden', minWidth: 0,
              }}>
                <div style={{
                  fontWeight: isActive ? 600 : 400, fontSize: '0.87rem', marginBottom: '2px',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  display: 'flex', alignItems: 'center', gap: '5px',
                }}>
                  <BookOpen size={12} style={{ flexShrink: 0, color: 'var(--fg-muted)' }} />
                  {skill.name}
                  {skill.type === 'legacy' && (
                    <span style={{ fontSize: '0.65rem', color: 'var(--fg-muted)', border: '1px solid var(--border)', borderRadius: '3px', padding: '0 3px' }}>legacy</span>
                  )}
                </div>
                {skill.description && (
                  <div style={{ fontSize: '0.73rem', color: 'var(--color-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {skill.description}
                  </div>
                )}
                {/* sub-item counts */}
                {(skill.references?.length > 0 || skill.scripts?.length > 0) && (
                  <div style={{ display: 'flex', gap: '8px', marginTop: '3px' }}>
                    {skill.references?.length > 0 && (
                      <span style={{ fontSize: '0.7rem', color: 'var(--fg-muted)', display: 'flex', alignItems: 'center', gap: '2px' }}>
                        <FileText size={9} /> {skill.references.length}
                      </span>
                    )}
                    {skill.scripts?.length > 0 && (
                      <span style={{ fontSize: '0.7rem', color: 'var(--fg-muted)', display: 'flex', alignItems: 'center', gap: '2px' }}>
                        <Code size={9} /> {skill.scripts.length}
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* ── RIGHT: editor ── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>
        {!hasEditor ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--color-muted)', fontSize: '0.9rem' }}>
            Select a skill or click <strong style={{ margin: '0 4px' }}>+</strong> to create one.
          </div>
        ) : (
          <>
            {/* ── Toolbar ── */}
            <div style={{
              padding: '0.55rem 1rem', borderBottom: '1px solid var(--color-border)',
              display: 'flex', alignItems: 'center', gap: '0.6rem',
              background: 'var(--color-midground)', flexWrap: 'wrap',
            }}>
              {creating ? (
                <>
                  <span style={{ fontWeight: 600, fontSize: '0.88rem' }}>✨ New Skill</span>
                  <input value={newSkillName} onChange={e => setNewSkillName(e.target.value)}
                    placeholder="skill name"
                    style={{ border: '1px solid var(--color-border)', borderRadius: '4px', padding: '3px 8px', fontSize: '0.85rem', width: '180px', background: 'var(--color-background)', color: 'var(--color-foreground)' }}
                  />
                  <button onClick={createSkill} disabled={saving || !newSkillName.trim()} style={{ background: newSkillName.trim() ? 'var(--btn-bg)' : 'var(--sidebar-active)', color: newSkillName.trim() ? 'var(--btn-fg)' : 'var(--fg-muted)', border: 'none', borderRadius: '4px', padding: '4px 12px', cursor: saving || !newSkillName.trim() ? 'default' : 'pointer', fontSize: '0.85rem' }}>
                    {saving ? 'Creating…' : 'Create'}
                  </button>
                  <button onClick={() => { setCreating(false); setEditorContent(''); }} style={{ background: 'transparent', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '4px 10px', cursor: 'pointer', fontSize: '0.85rem', color: 'var(--color-foreground)' }}>Cancel</button>
                </>
              ) : (
                <>
                  <span style={{ fontWeight: 600, fontSize: '0.88rem', flex: 1 }}>
                    {selected?.name}
                    {dirty && <span style={{ color: 'var(--fg-muted)', marginLeft: '6px', fontSize: '0.8rem' }}>●</span>}
                  </span>
                  {selected?.version && <span style={{ fontSize: '0.73rem', color: 'var(--color-muted)' }}>v{selected.version}</span>}
                  <span style={{ fontSize: '0.72rem', color: 'var(--fg-muted)', fontFamily: 'monospace', background: 'var(--sidebar-active)', padding: '1px 6px', borderRadius: '3px' }}>
                    📁 {selected?.folder || selected?.name}
                  </span>

                  {/* Tabs */}
                  <button style={tabBtn('skill')} onClick={() => switchTab('skill')}>
                    <BookOpen size={12} /> SKILL.md
                  </button>
                  <button style={tabBtn('references')} onClick={() => switchTab('references')}>
                    <FileText size={12} /> References
                    {selected && selected.references?.length > 0 && (
                      <span style={{ fontSize: '0.7rem', background: 'var(--border)', borderRadius: '8px', padding: '0 5px' }}>{selected.references.length}</span>
                    )}
                  </button>
                  <button style={tabBtn('scripts')} onClick={() => switchTab('scripts')}>
                    <Code size={12} /> Scripts
                    {selected && selected.scripts?.length > 0 && (
                      <span style={{ fontSize: '0.7rem', background: 'var(--border)', borderRadius: '8px', padding: '0 5px' }}>{selected.scripts.length}</span>
                    )}
                  </button>

                  {/* Actions for SKILL.md tab */}
                  {activeTab === 'skill' && <>
                    <button onClick={saveSkill} disabled={saving || !dirty} style={{ background: dirty ? 'var(--btn-bg)' : 'var(--color-midground)', color: dirty ? 'var(--btn-fg)' : 'var(--color-muted)', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '4px 12px', cursor: saving || !dirty ? 'default' : 'pointer', fontSize: '0.85rem' }}>
                      {saving ? 'Saving…' : 'Save'}
                    </button>
                    {deleteConfirm === selected?.name ? (
                      <>
                        <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>Delete?</span>
                        <button onClick={() => deleteSkill(selected!.name)} style={{ background: '#ef4444', color: '#fff', border: 'none', borderRadius: '4px', padding: '4px 10px', cursor: 'pointer', fontSize: '0.8rem' }}>Yes</button>
                        <button onClick={() => setDeleteConfirm(null)} style={{ background: 'transparent', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '0.8rem', color: 'var(--color-foreground)' }}>No</button>
                      </>
                    ) : (
                      <button onClick={() => setDeleteConfirm(selected?.name ?? null)} style={{ background: 'transparent', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '4px 8px', cursor: 'pointer', fontSize: '0.8rem', color: '#ef4444', display: 'flex', alignItems: 'center' }}>
                        <Trash2 size={13} />
                      </button>
                    )}
                  </>}
                </>
              )}

              {/* Save message */}
              {saveMsg && (
                <span style={{ fontSize: '0.8rem', color: saveMsg.ok ? '#22c55e' : '#ef4444' }}>
                  {saveMsg.ok ? '✓' : '✗'} {saveMsg.text}
                </span>
              )}
            </div>

            {/* ── SKILL.md editor ── */}
            {(activeTab === 'skill') && (
              <textarea value={editorContent} onChange={e => { setEditorContent(e.target.value); setDirty(true); setSaveMsg(null); }}
                spellCheck={false} style={{
                  flex: 1, width: '100%', resize: 'none', border: 'none', outline: 'none',
                  padding: '1rem', fontFamily: 'Consolas, "Courier New", monospace',
                  fontSize: '0.85rem', lineHeight: '1.6',
                  background: 'var(--color-background)', color: 'var(--color-foreground)',
                  boxSizing: 'border-box',
                }}
                placeholder="Skill content (Markdown with optional YAML frontmatter)…"
              />
            )}

            {/* ── References / Scripts tab ── */}
            {(activeTab === 'references' || activeTab === 'scripts') && !creating && (
              <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

                {/* File list */}
                <div style={{ width: '200px', flexShrink: 0, borderRight: '1px solid var(--color-border)', display: 'flex', flexDirection: 'column', background: 'var(--color-midground)' }}>
                  {/* New file bar */}
                  <div style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)', display: 'flex', gap: '4px' }}>
                    <input value={newFileName} onChange={e => setNewFileName(e.target.value)}
                      placeholder={activeTab === 'references' ? 'doc.md' : 'run.py'}
                      onKeyDown={e => e.key === 'Enter' && createFile()}
                      style={{ flex: 1, background: 'var(--color-background)', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '3px 6px', fontSize: '0.78rem', color: 'var(--color-foreground)', fontFamily: 'monospace' }}
                    />
                    <button onClick={createFile} disabled={!newFileName.trim() || fileSaving} title="Create file"
                      style={{ background: 'var(--btn-bg)', color: 'var(--btn-fg)', border: 'none', borderRadius: '4px', padding: '3px 7px', cursor: 'pointer', display: 'flex', alignItems: 'center' }}>
                      <Plus size={12} />
                    </button>
                  </div>

                  {/* File list */}
                  <div style={{ flex: 1, overflowY: 'auto', padding: '4px' }}>
                    {subFiles.length === 0 && (
                      <div style={{ padding: '10px', color: 'var(--fg-muted)', fontSize: '0.78rem', textAlign: 'center' }}>
                        No {activeTab} yet
                      </div>
                    )}
                    {subFiles.map(f => {
                      const isActive = selectedFile?.name === f.name;
                      return (
                        <div key={f.name} style={fileRow(isActive)} onClick={() => openFile(f)}>
                          {activeTab === 'references' ? <FileText size={12} style={{ flexShrink: 0, color: 'var(--fg-muted)' }} /> : <Code size={12} style={{ flexShrink: 0, color: 'var(--fg-muted)' }} />}
                          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</span>
                          <span style={{ fontSize: '0.65rem', color: 'var(--fg-muted)', flexShrink: 0 }}>{fmtSize(f.size)}</span>
                          {deletingFile === f.name ? (
                            <span style={{ display: 'flex', gap: '3px' }}>
                              <button onClick={e => { e.stopPropagation(); deleteFile(f.name); }} style={{ background: '#ef4444', color: '#fff', border: 'none', borderRadius: '3px', padding: '1px 5px', cursor: 'pointer', fontSize: '0.7rem' }}>✓</button>
                              <button onClick={e => { e.stopPropagation(); setDeletingFile(null); }} style={{ background: 'transparent', border: '1px solid var(--border)', borderRadius: '3px', padding: '1px 4px', cursor: 'pointer', fontSize: '0.7rem' }}>✗</button>
                            </span>
                          ) : (
                            <button onClick={e => { e.stopPropagation(); setDeletingFile(f.name); }} title="Delete" style={{ background: 'transparent', border: 'none', color: '#ef4444', cursor: 'pointer', padding: '0 2px', display: 'flex', alignItems: 'center' }}>
                              <Trash2 size={11} />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* File content editor */}
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                  {!selectedFile ? (
                    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--fg-muted)', fontSize: '0.85rem' }}>
                      Select a file or create one above
                    </div>
                  ) : (
                    <>
                      {/* File toolbar */}
                      <div style={{ padding: '5px 10px', borderBottom: '1px solid var(--color-border)', display: 'flex', alignItems: 'center', gap: '8px', background: 'var(--color-midground)' }}>
                        <span style={{ fontFamily: 'monospace', fontSize: '0.82rem', flex: 1, color: 'var(--fg)' }}>
                          {selectedFile.name}
                          {fileDirty && <span style={{ color: 'var(--fg-muted)', marginLeft: '5px' }}>●</span>}
                        </span>
                        <button onClick={saveFile} disabled={fileSaving || !fileDirty} style={{ background: fileDirty ? 'var(--btn-bg)' : 'var(--color-midground)', color: fileDirty ? 'var(--btn-fg)' : 'var(--color-muted)', border: '1px solid var(--color-border)', borderRadius: '4px', padding: '3px 10px', cursor: fileSaving || !fileDirty ? 'default' : 'pointer', fontSize: '0.82rem' }}>
                          {fileSaving ? 'Saving…' : 'Save'}
                        </button>
                        {fileMsg && <span style={{ fontSize: '0.78rem', color: fileMsg.ok ? '#22c55e' : '#ef4444' }}>{fileMsg.ok ? '✓' : '✗'} {fileMsg.text}</span>}
                      </div>
                      <textarea value={fileContent} onChange={e => { setFileContent(e.target.value); setFileDirty(true); setFileMsg(null); }}
                        spellCheck={false} style={{ flex: 1, width: '100%', resize: 'none', border: 'none', outline: 'none', padding: '1rem', fontFamily: 'Consolas, "Courier New", monospace', fontSize: '0.83rem', lineHeight: '1.6', background: 'var(--color-background)', color: 'var(--color-foreground)', boxSizing: 'border-box' }}
                      />
                    </>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
