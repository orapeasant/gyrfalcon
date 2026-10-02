import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, CircleCheck, Copy, Download, FileUp, LayoutGrid, List, MoreHorizontal, ParkingCircleOff, Pencil, Plus, Save, Search, Settings, SlidersHorizontal, Trash2, Workflow, X } from "lucide-react";
import { api } from "../lib/api";
import { AttributeEditor, FlowCanvasEditor } from "./FlowCanvasEditor";
import { canvasDraftIssue, type Activity, type FlowAttribute, type FlowGraph, type FlowNode, type GraphDefinition } from "./flowDesignerTypes";
import "./FlowDesignerPage.css";

type View = "list" | "icons";

const emptyGraph = (): FlowGraph => ({ version: 2, nodes: [
  { id: "start", type: "start", label: "Start", position: { x: 100, y: 180 }, transients: ["next"], attributes: [] },
  { id: "end", type: "end", label: "End", position: { x: 490, y: 180 }, transients: ["done"], attributes: [] },
], edges: [{ id: "start_to_end", from: "start", to: "end", transient: "next" }], attributes: [], edgeRouting: "bezier" });
const dateLabel = (timestamp: number) => new Date(timestamp * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const graphFingerprint = (value: FlowGraph) => JSON.stringify(value);

export function FlowDesignerPage() {
  const navigate = useNavigate();
  const [definitions, setDefinitions] = useState<GraphDefinition[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [propertiesId, setPropertiesId] = useState<string | null>(null);
  const [selectedLibraryId, setSelectedLibraryId] = useState<string | null>(null);
  const [graph, setGraph] = useState<FlowGraph>(emptyGraph());
  const [canvasSettingsOpen, setCanvasSettingsOpen] = useState(false);
  const [propertiesDraft, setPropertiesDraft] = useState<FlowGraph>(emptyGraph());
  const [propertiesName, setPropertiesName] = useState("");
  const [propertiesEnabled, setPropertiesEnabled] = useState(true);
  const [propertiesBaseline, setPropertiesBaseline] = useState("");
  const [view, setView] = useState<View>("list");
  const [search, setSearch] = useState("");
  const [menuId, setMenuId] = useState<string | null>(null);
  const rowImportFileRef = useRef<HTMLInputElement>(null);
  const rowImportTargetRef = useRef<GraphDefinition | null>(null);
  const canvasImportFileRef = useRef<HTMLInputElement>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [renameTarget, setRenameTarget] = useState<GraphDefinition | null>(null);
  const [renameName, setRenameName] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [savedGraphSnapshot, setSavedGraphSnapshot] = useState("");
  const [codeDirty, setCodeDirty] = useState(false);
  const codeSave = useRef<() => Promise<boolean>>(async () => true);
  const [leavePromptOpen, setLeavePromptOpen] = useState(false);
  const [leaveError, setLeaveError] = useState("");
  const leaveAction = useRef<(() => void) | null>(null);
  const [breadcrumbTargets, setBreadcrumbTargets] = useState<{ tail: HTMLElement; actions: HTMLElement } | null>(null);

  useEffect(() => {
    const tail = document.getElementById("flow-breadcrumb-tail");
    const actions = document.getElementById("flow-breadcrumb-actions");
    if (tail && actions) setBreadcrumbTargets({ tail, actions });
  }, []);

  const selected = definitions.find((definition) => definition.id === selectedId);
  const propertyDefinition = definitions.find((definition) => definition.id === propertiesId);
  const dirty = !!selectedId && (graphFingerprint(graph) !== savedGraphSnapshot || codeDirty);
  const onCodeEditorDirtyChange = useCallback((pending: boolean, save: () => Promise<boolean>) => {
    setCodeDirty(pending);
    codeSave.current = save;
  }, []);
  const visible = useMemo(() => definitions.filter((definition) => definition.name.toLowerCase().includes(search.trim().toLowerCase())), [definitions, search]);

  async function load(selectId?: string) {
    const [graphs, catalog] = await Promise.all([api.getFlowGraphs(), api.getGraphActivities()]);
    const rows = (graphs.graphs || []) as GraphDefinition[];
    setDefinitions(rows);
    setActivities(catalog.activities || []);
    if (selectId) {
      const found = rows.find((item) => item.id === selectId);
      if (found) openEditor(found);
    }
  }

  useEffect(() => { load().catch((error) => setMessage(String(error))); }, []);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    const interceptNavigation = (event: globalThis.MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (!(event.target instanceof Element)) return;
      const target = event.target;
      const anchor = target.closest<HTMLAnchorElement>("a[href]");
      if (!anchor || anchor.target === "_blank") return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || url.pathname === window.location.pathname) return;
      event.preventDefault();
      event.stopPropagation();
      leaveAction.current = () => navigate(`${url.pathname}${url.search}${url.hash}`);
      setLeaveError("");
      setLeavePromptOpen(true);
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", interceptNavigation, true);
    return () => { window.removeEventListener("beforeunload", beforeUnload); document.removeEventListener("click", interceptNavigation, true); };
  }, [dirty, navigate]);
  useEffect(() => {
    if (!menuId) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as Element).closest("[data-flow-menu]")) setMenuId(null);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuId(null); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [menuId]);

  function openEditor(definition: GraphDefinition) {
    setPropertiesId(null);
    setCanvasSettingsOpen(false);
    setSelectedId(definition.id);
    setSelectedLibraryId(definition.id);
    const draft = structuredClone(definition.draft);
    const editable = { ...draft, edges: draft.edges.map((edge, index) => {
      const normalized = { ...edge, id: edge.id || `legacy_${index}_${edge.from}_${edge.to}` } as typeof edge & { sourceHandle?: string; targetHandle?: string };
      delete normalized.sourceHandle;
      delete normalized.targetHandle;
      return normalized;
    }) };
    setGraph(editable);
    setSavedGraphSnapshot(graphFingerprint(editable));
    setMenuId(null);
    setMessage("");
  }

  function openProperties(definition: GraphDefinition) {
    setMenuId(null);
    setSelectedId(null);
    setPropertiesId(definition.id);
    setSelectedLibraryId(definition.id);
    const draft = structuredClone(definition.draft);
    setPropertiesDraft(draft);
    setPropertiesName(definition.name);
    setPropertiesEnabled(definition.enabled !== false);
    setPropertiesBaseline(JSON.stringify({ name: definition.name, enabled: definition.enabled !== false, draft }));
    setMessage("");
  }

  function openFlowDesign(definition: GraphDefinition) {
    if (propertiesId && propertyDirty && !window.confirm("Discard unsaved flow properties and open the designer?")) return;
    setPropertiesId(null);
    openEditor(definition);
  }

  async function saveProperties() {
    if (!propertyDefinition) return;
    const name = propertiesName.trim();
    if (!name) { setMessage("Enter a flow name first."); return; }
    setBusy(true);
    try {
      await api.saveFlowGraphDraft(propertyDefinition.id, propertiesDraft);
      if (name !== propertyDefinition.name) await api.renameFlowGraph(propertyDefinition.id, name);
      if (propertiesEnabled !== (propertyDefinition.enabled !== false)) await api.setFlowGraphEnabled(propertyDefinition.id, propertiesEnabled);
      await load();
      setPropertiesBaseline(JSON.stringify({ name, enabled: propertiesEnabled, draft: propertiesDraft }));
      setMessage("Flow properties saved.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  const propertyDirty = !!propertiesId && JSON.stringify({ name: propertiesName, enabled: propertiesEnabled, draft: propertiesDraft }) !== propertiesBaseline;

  async function create() {
    if (!newName.trim()) { setMessage("Enter a flow name first."); return; }
    setBusy(true);
    try {
      const result = await api.createFlowGraph(newName.trim(), emptyGraph());
      await load(result.id);
      setNewName("");
      setCreating(false);
      setMessage("Draft created. Drag an activity or Process onto the canvas.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  async function renameDefinition() {
    if (!renameTarget) return;
    const name = renameName.trim();
    if (!name) { setMessage("Enter a flow name first."); return; }
    if (definitions.some((item) => item.id !== renameTarget.id && item.name === name)) {
      setMessage(`A flow named “${name}” already exists.`);
      return;
    }
    setBusy(true);
    try {
      await api.renameFlowGraph(renameTarget.id, name);
      await load();
      setRenameTarget(null);
      setMessage(`Renamed flow to “${name}”. Its internal ID is unchanged.`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  async function duplicateDefinition(definition: GraphDefinition) {
    setMenuId(null);
    const baseName = `${definition.name} (copy)`;
    let name = baseName;
    let suffix = 2;
    while (definitions.some((item) => item.name === name)) name = `${definition.name} (copy ${suffix++})`;
    setBusy(true);
    try {
      const result = await api.createFlowGraph(name, structuredClone(definition.draft));
      await load();
      setSelectedLibraryId(result.id);
      setMessage(`Created “${name}”.`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  function exportDefinition(definition: GraphDefinition) {
    setMenuId(null);
    const contents = JSON.stringify({
      format: "gyrfalcon-flow",
      format_version: 1,
      name: definition.name,
      draft: definition.draft,
      exported_at: new Date().toISOString(),
    }, null, 2);
    const url = URL.createObjectURL(new Blob([contents], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${definition.name.trim().replace(/[^a-z0-9_-]+/gi, "-").replace(/^-|-$/g, "") || "flow"}.gyrfalcon-flow.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    setMessage(`Exported “${definition.name}”.`);
  }

  async function importDefinition(file: File, definition: GraphDefinition) {
    setMenuId(null);
    if (!window.confirm(`Replace the current design of “${definition.name}” with the imported flow draft?`)) {
      if (rowImportFileRef.current) rowImportFileRef.current.value = "";
      return;
    }
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text()) as { format?: string; format_version?: number; draft?: FlowGraph };
      if (payload.format !== "gyrfalcon-flow" || payload.format_version !== 1) {
        throw new Error("This is not a supported Gyrfalcon flow export.");
      }
      if (!payload.draft || !Array.isArray(payload.draft.nodes) || !Array.isArray(payload.draft.edges)) {
        throw new Error("The flow export is missing its graph draft.");
      }
      await api.saveFlowGraphDraft(definition.id, payload.draft);
      await load();
      setMessage(`Imported draft and replaced the current design of “${definition.name}”.`);
    } catch (error) {
      setMessage(`Could not import flow: ${String(error)}`);
    } finally {
      setBusy(false);
      if (rowImportFileRef.current) rowImportFileRef.current.value = "";
      rowImportTargetRef.current = null;
    }
  }

  function exportCanvasDraft() {
    if (!selected) return;
    const contents = JSON.stringify({
      format: "gyrfalcon-flow",
      format_version: 1,
      name: selected.name,
      draft: graph,
      exported_at: new Date().toISOString(),
    }, null, 2);
    const url = URL.createObjectURL(new Blob([contents], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${selected.name.trim().replace(/[^a-z0-9_-]+/gi, "-").replace(/^-|-$/g, "") || "flow"}.gyrfalcon-flow.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setMessage(`Exported current draft of “${selected.name}”.`);
  }

  async function importCanvasDraft(file: File) {
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text()) as { format?: string; format_version?: number; draft?: FlowGraph };
      if (payload.format !== "gyrfalcon-flow" || payload.format_version !== 1 || !payload.draft || !Array.isArray(payload.draft.nodes) || !Array.isArray(payload.draft.edges)) {
        throw new Error("This is not a supported Gyrfalcon flow export.");
      }
      if (dirty && !window.confirm("Replace the current canvas with this imported draft? Unsaved canvas changes will be replaced.")) return;
      setGraph(structuredClone(payload.draft));
      setMessage("Imported draft into the canvas. Save it to update this flow.");
    } catch (error) {
      setMessage(`Could not import flow: ${String(error)}`);
    } finally {
      setBusy(false);
      if (canvasImportFileRef.current) canvasImportFileRef.current.value = "";
    }
  }

  async function unpublish() {
    if (!selectedId || !selected?.published_version) return;
    setBusy(true);
    try {
      await api.unpublishFlowGraph(selectedId);
      await load();
      setMessage("Flow unpublished. Existing run history and version snapshots are retained.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  async function save(publish: boolean): Promise<boolean> {
    if (!selectedId) return false;
    setBusy(true);
    try {
      if (codeDirty && !(await codeSave.current())) throw new Error("Python code could not be saved. Review the code editor.");
      const draft = graph;
      const issue = canvasDraftIssue(draft);
      if (publish && issue) throw new Error(issue);
      await api.saveFlowGraphDraft(selectedId, draft);
      setSavedGraphSnapshot(graphFingerprint(draft));
      let result: { version: number } | null = null;
      if (publish) {
        try { result = await api.publishFlowGraph(selectedId); }
        catch (error) { setMessage(`Draft saved, but publishing failed: ${String(error)}`); return false; }
      }
      let refreshError = "";
      try { await load(); } catch (error) { refreshError = ` Could not refresh the flow list: ${String(error)}`; }
      setMessage((result ? `Published version ${result.version}.` : issue ? `Draft saved. Design issue: ${issue}` : "Draft saved.") + refreshError);
      return true;
    } catch (error) { setMessage(String(error)); return false; }
    finally { setBusy(false); }
  }
  const requestLeave = (action: () => void) => {
    if (!dirty) { action(); return; }
    leaveAction.current = action;
    setLeaveError("");
    setLeavePromptOpen(true);
  };
  const finishLeave = async (choice: "save" | "publish" | "discard") => {
    if (choice !== "discard") {
      const success = await save(choice === "publish");
      if (!success) { setLeaveError("Could not save the flow. Review the message and try again."); return; }
    }
    setLeavePromptOpen(false);
    leaveAction.current?.();
    leaveAction.current = null;
  };

  async function deleteDefinition(definition: GraphDefinition) {
    setMenuId(null);
    if (!window.confirm(`Delete “${definition.name}” and all its published versions? Past run records will remain.`)) return;
    setBusy(true);
    try {
      await api.deleteFlowGraph(definition.id);
      if (selectedId === definition.id) setSelectedId(null);
      if (selectedLibraryId === definition.id) setSelectedLibraryId(null);
      await load();
      setMessage(`Deleted “${definition.name}”.`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  function actions(definition: GraphDefinition) {
    return <div className="flow-library-actions" onClick={(event) => event.stopPropagation()} onDoubleClick={(event) => event.stopPropagation()}>
      <div className="flow-menu-anchor" data-flow-menu>
        <button className="flow-icon-button" type="button" aria-label={`More actions for ${definition.name}`} aria-expanded={menuId === definition.id}
          onClick={() => setMenuId(menuId === definition.id ? null : definition.id)}><MoreHorizontal size={18} /></button>
        {menuId === definition.id && <div className="flow-menu" role="menu">
          <button role="menuitem" type="button" onClick={() => openProperties(definition)}><SlidersHorizontal size={14} /> Edit</button>
          <button role="menuitem" type="button" onClick={() => openFlowDesign(definition)}><Workflow size={14} /> Design</button>
          <button role="menuitem" type="button" onClick={() => { setMenuId(null); setRenameTarget(definition); setRenameName(definition.name); setMessage(""); }}><Pencil size={14} /> Rename</button>
          <button role="menuitem" type="button" disabled={busy} onClick={() => duplicateDefinition(definition)}><Copy size={14} /> Duplicate</button>
          <button role="menuitem" type="button" disabled={busy} onClick={() => { setMenuId(null); rowImportTargetRef.current = definition; rowImportFileRef.current?.click(); }}><FileUp size={14} /> Import</button>
          <button role="menuitem" type="button" disabled={busy} onClick={() => exportDefinition(definition)}><Download size={14} /> Export</button>
          <button role="menuitem" type="button" className="flow-danger" onClick={() => deleteDefinition(definition)}><Trash2 size={14} /> Delete</button>
        </div>}
      </div>
    </div>;
  }

  return <div className={`flow-designer-page ${selectedId || propertiesId ? "is-editing" : ""}`}>
    {selectedId ? <>
      {breadcrumbTargets && createPortal(<><span className="flow-breadcrumb-separator"> / </span><span className="flow-breadcrumb-name" title={selected?.name || "Flow"}>{selected?.name || "Flow"}</span></>, breadcrumbTargets.tail)}
      {breadcrumbTargets && createPortal(<div className="flow-toolbar-actions">
          <button className="flow-icon-button" type="button" aria-label="Back to flows" title="Back to flows" onClick={() => requestLeave(() => { setCanvasSettingsOpen(false); setSelectedId(null); setMessage(""); })}><ArrowLeft size={16} /></button>
          <button className="flow-icon-button" type="button" aria-label="Save draft" title={dirty ? "Save unsaved changes" : "Save draft"} disabled={busy} onClick={() => save(false)}><Save size={16} />{dirty && <span className="flow-unsaved-dot" />}</button>
          <button className="flow-icon-button" type="button" aria-label="Import flow draft" title="Import flow draft" disabled={busy} onClick={() => canvasImportFileRef.current?.click()}><FileUp size={16} /></button>
          <button className="flow-icon-button" type="button" aria-label="Export flow draft" title="Export current draft" disabled={busy} onClick={exportCanvasDraft}><Download size={16} /></button>
          <button className="flow-icon-button" type="button" aria-label="Flow settings" title="Flow settings" disabled={busy} onClick={() => setCanvasSettingsOpen(true)}><Settings size={16} /></button>
          <button className="flow-icon-button flow-icon-primary" type="button" aria-label="Publish flow" title="Publish flow" disabled={busy} onClick={() => save(true)}><CircleCheck size={17} /></button>
          {!!selected?.published_version && <button className="flow-icon-button" type="button" aria-label="Unpublish flow" title="Unpublish flow" disabled={busy} onClick={unpublish}><ParkingCircleOff size={17} /></button>}
        </div>, breadcrumbTargets.actions)}
      <input ref={canvasImportFileRef} type="file" accept=".json,application/json" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void importCanvasDraft(file); }} />
      <FlowCanvasEditor graph={graph} setGraph={setGraph} activities={activities} onActivitiesChange={setActivities} onCodeEditorDirtyChange={onCodeEditorDirtyChange} definitions={definitions} currentDefinitionId={selectedId} onOpenAgent={(agentId) => {
        void save(false).then((saved) => { if (saved) navigate(`/agents?id=${encodeURIComponent(agentId)}`); });
      }} onMessage={setMessage} />
    </> : propertiesId && propertyDefinition ? <>
      <header className="flow-page-header">
        <div className="flow-properties-page-title"><button className="flow-icon-button" type="button" aria-label="Back to flows" title="Back to flows" onClick={() => { if (!propertyDirty || window.confirm("Discard unsaved flow properties?")) { setPropertiesId(null); setMessage(""); } }}><ArrowLeft size={16} /></button><div><h1>Flow properties</h1><p>{propertyDefinition.name}</p></div></div>
        <div className="flow-toolbar-actions"><button className="flow-icon-button" type="button" aria-label="Design flow" title="Design flow" onClick={() => openFlowDesign(propertyDefinition)}><Workflow size={16} /> Design</button><button className="flow-icon-button flow-icon-primary" type="button" aria-label="Save flow properties" title="Save flow properties" disabled={busy || !propertyDirty} onClick={saveProperties}><Save size={16} />{propertyDirty && <span className="flow-unsaved-dot" />}</button></div>
      </header>
      <div className="flow-properties-form">
        <label>Flow name<input value={propertiesName} onChange={(event) => setPropertiesName(event.target.value)} /></label>
        <label className="flow-check"><input type="checkbox" checked={propertiesEnabled} onChange={(event) => setPropertiesEnabled(event.target.checked)} /> Enabled</label>
        <p className="flow-hint">Disabled flows remain saved but cannot be invoked.</p>
        <AttributeEditor attributes={propertiesDraft.attributes || []} onChange={(attributes: FlowAttribute[]) => setPropertiesDraft((current) => ({ ...current, attributes }))} onMessage={setMessage} />
        <section className="flow-properties-activities"><h2>Activities</h2>{propertiesDraft.nodes.length ? <ul>{propertiesDraft.nodes.map((node: FlowNode) => <li key={node.id}><span>{node.label || node.id}</span><small>{node.type === "python" ? node.implementation === "agent" ? "Agent" : node.activity || "Activity" : node.type[0].toUpperCase() + node.type.slice(1)}</small></li>)}</ul> : <p className="flow-hint">No activities in this flow yet.</p>}</section>
      </div>
    </> : <>
      <header className="flow-page-header">
        <div><h1>Flow designer</h1><p>Design, publish, and run your flows.</p></div>
        <div className="flow-toolbar-actions">
          <input ref={rowImportFileRef} type="file" accept=".json,.gyrfalcon-flow.json,application/json" hidden onChange={(event) => { const file = event.target.files?.[0]; const definition = rowImportTargetRef.current; if (file && definition) void importDefinition(file, definition); }} />
          <button className="flow-icon-button flow-icon-primary" type="button" aria-label="New flow" title="New flow" onClick={() => { setMessage(""); setCreating(true); }}><Plus size={18} /></button>
        </div>
      </header>
      <div className="flow-library-toolbar">
        <div className="flow-search"><Search size={16} /><input aria-label="Search flows" placeholder="Search flows…" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
        <span className="flow-count">{visible.length} {visible.length === 1 ? "flow" : "flows"}</span>
        <div className="flow-view-toggle" role="group" aria-label="Flow view">
          <button type="button" aria-label="List view" aria-pressed={view === "list"} className={view === "list" ? "active" : ""} onClick={() => setView("list")}><List size={16} /></button>
          <button type="button" aria-label="Icon view" aria-pressed={view === "icons"} className={view === "icons" ? "active" : ""} onClick={() => setView("icons")}><LayoutGrid size={16} /></button>
        </div>
      </div>
      {view === "list" ? <div className="flow-table-wrap"><table className="flow-library-table">
        <thead><tr><th>Flow</th><th>Activities</th><th>Status</th><th>Updated</th><th className="flow-actions-heading">Actions</th></tr></thead>
        <tbody>{visible.map((definition) => <tr key={definition.id} className={selectedLibraryId === definition.id ? "flow-library-selected" : ""}
          tabIndex={0} aria-selected={selectedLibraryId === definition.id} title="Double-click to edit"
          onClick={() => setSelectedLibraryId(definition.id)} onDoubleClick={() => openProperties(definition)}
          onKeyDown={(event) => { if (event.target === event.currentTarget && event.key === "Enter") openProperties(definition); }}>
          <td><span className="flow-name-link"><span className="flow-row-icon"><Workflow size={16} /></span><span>{definition.name}</span></span></td>
          <td>{definition.draft.nodes.length}</td>
          <td><span className={`flow-status ${definition.enabled === false ? "draft" : definition.published_version ? "published" : "draft"}`}>{definition.enabled === false ? "Disabled" : definition.published_version ? `Published v${definition.published_version}` : "Draft"}</span></td>
          <td className="flow-date">{dateLabel(definition.updated_at)}</td>
          <td>{actions(definition)}</td>
        </tr>)}</tbody>
      </table></div> : <div className="flow-icon-grid">{visible.map((definition) => <article className={`flow-tile ${selectedLibraryId === definition.id ? "flow-library-selected" : ""}`} key={definition.id}
        tabIndex={0} aria-selected={selectedLibraryId === definition.id} title="Double-click to edit"
        onClick={() => setSelectedLibraryId(definition.id)} onDoubleClick={() => openProperties(definition)}
        onKeyDown={(event) => { if (event.target === event.currentTarget && event.key === "Enter") openProperties(definition); }}>
        <div className="flow-tile-top"><span className="flow-tile-icon"><Workflow size={20} /></span>{actions(definition)}</div>
        <span className="flow-tile-name">{definition.name}</span>
        <div className="flow-tile-meta"><span>{definition.draft.nodes.length} activities</span><span className={`flow-status ${definition.enabled !== false && definition.published_version ? "published" : "draft"}`}>{definition.enabled === false ? "Disabled" : definition.published_version ? `Published v${definition.published_version}` : "Draft"}</span></div>
        <small>Updated {dateLabel(definition.updated_at)}</small>
      </article>)}</div>}
      {visible.length === 0 && <div className="flow-library-empty"><Workflow size={28} /><strong>{search ? "No matching flows" : "No flows yet"}</strong><span>{search ? "Try another search." : "Create your first flow."}</span></div>}
    </>}

    {message && <div className="flow-message" role="status">{message}<button type="button" aria-label="Dismiss message" onClick={() => setMessage("")}><X size={14} /></button></div>}

    {leavePromptOpen && <div className="flow-modal-backdrop"><div className="flow-modal" role="dialog" aria-modal="true" aria-label="Unsaved flow changes"><div className="flow-modal-heading"><h2>Unsaved flow changes</h2></div><p className="flow-hint">Save your changes before leaving this flow.</p>{leaveError && <p className="flow-modal-error" role="alert">{leaveError}</p>}<div className="flow-modal-actions flow-leave-actions"><button type="button" onClick={() => { setLeavePromptOpen(false); leaveAction.current = null; }}>Stay</button><button type="button" onClick={() => finishLeave("discard")}>Discard</button><button type="button" disabled={busy} onClick={() => finishLeave("save")}>Save draft</button><button type="button" disabled={busy} title="Save and publish" onClick={() => finishLeave("publish")}>Save and publish</button></div></div></div>}

    {canvasSettingsOpen && selectedId && <div className="flow-modal-backdrop" onClick={() => setCanvasSettingsOpen(false)}><div className="flow-modal" role="dialog" aria-modal="true" aria-label="Flow settings" onClick={(event) => event.stopPropagation()}>
      <div className="flow-modal-heading"><h2>Flow settings</h2><button className="flow-icon-button" type="button" aria-label="Close settings" onClick={() => setCanvasSettingsOpen(false)}><X size={17} /></button></div>
      <label>Global edge type<select value={graph.edgeRouting || "bezier"} onChange={(event) => {
        const routing = event.target.value as NonNullable<FlowGraph["edgeRouting"]>;
        setGraph((current) => ({ ...current, edgeRouting: routing, edges: current.edges.map((edge) => ({ ...edge, routing })) }));
      }}><option value="bezier">Bezier</option><option value="straight">Straight</option><option value="step">Step</option><option value="smoothstep">Smooth step</option></select></label>
      <p className="flow-hint">Applies to every connection in this flow, including connections created later. Save the draft to keep this setting.</p>
      <div className="flow-modal-actions"><button className="flow-icon-button" type="button" aria-label="Close settings" title="Close" onClick={() => setCanvasSettingsOpen(false)}><Check size={17} /></button></div>
    </div></div>}

    {creating && <div className="flow-modal-backdrop" onClick={() => setCreating(false)}><div className="flow-modal" role="dialog" aria-modal="true" aria-label="Create flow" onClick={(event) => event.stopPropagation()}>
      <div className="flow-modal-heading"><h2>Create flow</h2><button className="flow-icon-button" aria-label="Close" onClick={() => setCreating(false)}><X size={17} /></button></div>
      <label>Flow name<input autoFocus placeholder="e.g. Daily report" value={newName} onChange={(event) => setNewName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void create(); }} /></label>
      {message && <p className="flow-modal-error" role="alert">{message}</p>}
      <div className="flow-modal-actions"><button className="flow-icon-button" aria-label="Cancel" title="Cancel" onClick={() => setCreating(false)}><X size={17} /></button><button className="flow-icon-button flow-icon-primary" aria-label="Create flow" title="Create flow" disabled={busy} onClick={create}><Check size={18} /></button></div>
    </div></div>}

    {renameTarget && <div className="flow-modal-backdrop" onClick={() => setRenameTarget(null)}><div className="flow-modal" role="dialog" aria-modal="true" aria-label={`Rename ${renameTarget.name}`} onClick={(event) => event.stopPropagation()}>
      <div className="flow-modal-heading"><h2>Rename flow</h2><button className="flow-icon-button" aria-label="Close" onClick={() => setRenameTarget(null)}><X size={17} /></button></div>
      <label>Display name<input autoFocus value={renameName} onChange={(event) => setRenameName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void renameDefinition(); }} /></label>
      <p className="flow-hint">Renaming changes the display name. The flow’s internal ID stays the same.</p>
      <div className="flow-modal-actions"><button className="flow-icon-button" aria-label="Cancel" title="Cancel" onClick={() => setRenameTarget(null)}><X size={17} /></button><button className="flow-icon-button flow-icon-primary" aria-label="Save name" title="Save name" disabled={busy} onClick={renameDefinition}><Check size={18} /></button></div>
    </div></div>}

  </div>;
}
