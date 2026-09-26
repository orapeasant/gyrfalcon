import React, { useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, LayoutGrid, List, MoreHorizontal, Pencil, Play, Plus, Save, Search, Trash2, Upload, Workflow, X } from "lucide-react";
import { api } from "../lib/api";
import "./FlowDesignerPage.css";

type Node = { id: string; type: "python"; activity: string; version: string; inputs: Record<string, unknown> };
type Edge = { from: string; to: string; when?: { path: string; op: string; value: unknown } };
type Graph = { nodes: Node[]; edges: Edge[] };
type Definition = { id: string; name: string; draft: Graph; published_version: number | null; created_at: number; updated_at: number };
type Activity = { name: string; version: string };
type View = "list" | "icons";

const emptyGraph = (): Graph => ({ nodes: [], edges: [] });
const edgesFor = (nodes: Node[]): Edge[] => nodes.slice(0, -1).map((node, index) => ({ from: node.id, to: nodes[index + 1].id }));
const dateLabel = (timestamp: number) => new Date(timestamp * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function FlowDesignerPage() {
  const [definitions, setDefinitions] = useState<Definition[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedLibraryId, setSelectedLibraryId] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [graph, setGraph] = useState<Graph>(emptyGraph());
  const [view, setView] = useState<View>("list");
  const [search, setSearch] = useState("");
  const [menuId, setMenuId] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [inputsText, setInputsText] = useState("{}");
  const [runTarget, setRunTarget] = useState<Definition | null>(null);
  const [runInputs, setRunInputs] = useState('{"name":"world"}');
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const selected = definitions.find((definition) => definition.id === selectedId);
  const node = graph.nodes.find((item) => item.id === selectedNode);
  const visible = useMemo(() => definitions.filter((definition) => definition.name.toLowerCase().includes(search.trim().toLowerCase())), [definitions, search]);

  async function load(selectId?: string) {
    const [graphs, catalog] = await Promise.all([api.getFlowGraphs(), api.getGraphActivities()]);
    const rows = (graphs.graphs || []) as Definition[];
    setDefinitions(rows);
    setActivities(catalog.activities || []);
    if (selectId) {
      const found = rows.find((item) => item.id === selectId);
      if (found) openEditor(found);
    }
  }

  useEffect(() => { load().catch((error) => setMessage(String(error))); }, []);
  useEffect(() => { setInputsText(JSON.stringify(node?.inputs || {}, null, 2)); }, [selectedNode]);
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

  function openEditor(definition: Definition) {
    setSelectedId(definition.id);
    setSelectedLibraryId(definition.id);
    setGraph(structuredClone(definition.draft));
    setSelectedNode(definition.draft.nodes[0]?.id || null);
    setInputsText(JSON.stringify(definition.draft.nodes[0]?.inputs || {}, null, 2));
    setMenuId(null);
    setMessage("");
  }

  function openRun(definition: Definition) {
    setMessage("");
    setRunTarget(definition);
  }

  function updateNode(id: string, change: Partial<Node>) {
    setGraph((current) => ({ ...current, nodes: current.nodes.map((item) => item.id === id ? { ...item, ...change } : item) }));
  }

  function addNode() {
    const activity = activities[0];
    if (!activity) { setMessage("No registered activities are available."); return; }
    const ids = new Set(graph.nodes.map((item) => item.id));
    let count = graph.nodes.length + 1;
    while (ids.has(`step${count}`)) count++;
    const newNode: Node = { id: `step${count}`, type: "python", activity: activity.name, version: activity.version, inputs: {} };
    setGraph((current) => { const nodes = [...current.nodes, newNode]; return { nodes, edges: edgesFor(nodes) }; });
    setSelectedNode(newNode.id);
  }

  function reorder(index: number, direction: number) {
    const next = index + direction;
    if (next < 0 || next >= graph.nodes.length) return;
    const nodes = [...graph.nodes];
    [nodes[index], nodes[next]] = [nodes[next], nodes[index]];
    setGraph({ nodes, edges: edgesFor(nodes) });
  }

  function removeNode(id: string) {
    const nodes = graph.nodes.filter((item) => item.id !== id);
    setGraph({ nodes, edges: edgesFor(nodes) });
    setSelectedNode(nodes[0]?.id || null);
  }

  function graphWithEditor(): Graph {
    if (!node) return graph;
    const parsed = JSON.parse(inputsText);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Activity inputs must be a JSON object.");
    return { ...graph, nodes: graph.nodes.map((item) => item.id === node.id ? { ...item, inputs: parsed } : item) };
  }

  async function create() {
    if (!newName.trim()) { setMessage("Enter a flow name first."); return; }
    setBusy(true);
    try {
      const result = await api.createFlowGraph(newName.trim(), emptyGraph());
      await load(result.id);
      setNewName("");
      setCreating(false);
      setMessage("Draft created. Add an activity to begin.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  async function save(publish: boolean) {
    if (!selectedId) return;
    setBusy(true);
    try {
      const draft = graphWithEditor();
      await api.saveFlowGraphDraft(selectedId, draft);
      const result = publish ? await api.publishFlowGraph(selectedId) : null;
      setGraph(draft);
      await load(selectedId);
      setMessage(result ? `Published version ${result.version}.` : "Draft saved.");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  async function deleteDefinition(definition: Definition) {
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

  async function run() {
    if (!runTarget) return;
    setBusy(true);
    try {
      const parsed = JSON.parse(runInputs);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Run inputs must be a JSON object.");
      const result = await api.invokeFlowGraph(runTarget.id, parsed);
      setMessage(`Run ${result.run_id}: ${JSON.stringify(result.result)}`);
      setRunTarget(null);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  }

  function actions(definition: Definition) {
    return <div className="flow-library-actions" onClick={(event) => event.stopPropagation()} onDoubleClick={(event) => event.stopPropagation()}>
      <button className="flow-icon-button" type="button" title={definition.published_version ? `Run ${definition.name}` : "Publish before running"}
        aria-label={`Run ${definition.name}`} disabled={!definition.published_version || busy} onClick={() => openRun(definition)}><Play size={15} fill="currentColor" /></button>
      <div className="flow-menu-anchor" data-flow-menu>
        <button className="flow-icon-button" type="button" aria-label={`More actions for ${definition.name}`} aria-expanded={menuId === definition.id}
          onClick={() => setMenuId(menuId === definition.id ? null : definition.id)}><MoreHorizontal size={18} /></button>
        {menuId === definition.id && <div className="flow-menu" role="menu">
          <button role="menuitem" type="button" onClick={() => openEditor(definition)}><Pencil size={14} /> Edit</button>
          <button role="menuitem" type="button" className="flow-danger" onClick={() => deleteDefinition(definition)}><Trash2 size={14} /> Delete</button>
        </div>}
      </div>
    </div>;
  }

  return <div className="flow-designer-page">
    {selectedId ? <>
      <header className="flow-page-header flow-editor-header">
        <div className="flow-title-group">
          <button className="flow-icon-button" type="button" aria-label="Back to flows" title="Back to flows" onClick={() => { setSelectedId(null); setSelectedNode(null); setMessage(""); }}><ArrowLeft size={18} /></button>
          <div><h1>{selected?.name || "Flow"}</h1><p>{selected?.published_version ? `Published v${selected.published_version}` : "Draft"} · {graph.nodes.length} activities</p></div>
        </div>
        <div className="flow-toolbar-actions">
          <button className="flow-icon-button" type="button" aria-label="Add activity" title="Add activity" disabled={busy} onClick={addNode}><Plus size={17} /></button>
          <button className="flow-icon-button" type="button" aria-label="Save draft" title="Save draft" disabled={busy} onClick={() => save(false)}><Save size={16} /></button>
          <button className="flow-icon-button flow-icon-primary" type="button" aria-label="Publish flow" title="Publish flow" disabled={busy} onClick={() => save(true)}><Upload size={17} /></button>
        </div>
      </header>
      {selected && <div className="flow-detail-strip" aria-label="Flow details">
        <div><span>Flow ID</span><strong className="flow-detail-id" title={selected.id}>{selected.id}</strong></div>
        <div><span>Status</span><strong>{selected.published_version ? `Published v${selected.published_version}` : "Draft"}</strong></div>
        <div><span>Activities</span><strong>{graph.nodes.length}</strong></div>
        <div><span>Created</span><strong>{dateLabel(selected.created_at)}</strong></div>
        <div><span>Updated</span><strong>{dateLabel(selected.updated_at)}</strong></div>
      </div>}
      <div className="flow-editor">
        <section className="flow-canvas" aria-label="Flow activities">
          {graph.nodes.length === 0 && <div className="flow-empty-canvas"><Workflow size={26} /><strong>No activities yet</strong><span>Add an activity to start designing this flow.</span></div>}
          {graph.nodes.map((item, index) => <div className="flow-node-wrap" key={item.id}>
            {index > 0 && <div className="flow-node-line"><ArrowDown size={13} /></div>}
            <div className={`flow-node ${selectedNode === item.id ? "flow-node-selected" : ""}`} onClick={() => setSelectedNode(item.id)}>
              <div className="flow-node-icon"><Workflow size={16} /></div>
              <div className="flow-node-copy"><strong>{item.id}</strong><span>{item.activity} · v{item.version}</span></div>
              <div className="flow-node-actions">
                <button className="flow-icon-button" title="Move up" aria-label={`Move ${item.id} up`} onClick={(event) => { event.stopPropagation(); reorder(index, -1); }}><ArrowUp size={14} /></button>
                <button className="flow-icon-button" title="Move down" aria-label={`Move ${item.id} down`} onClick={(event) => { event.stopPropagation(); reorder(index, 1); }}><ArrowDown size={14} /></button>
                <button className="flow-icon-button" title="Remove activity" aria-label={`Remove ${item.id}`} onClick={(event) => { event.stopPropagation(); removeNode(item.id); }}><Trash2 size={14} /></button>
              </div>
            </div>
          </div>)}
        </section>
        <aside className="flow-inspector">
          <h2>Activity settings</h2>
          {node ? <>
            <label>Node ID<input value={node.id} onChange={(event) => {
              const oldId = node.id; const id = event.target.value;
              if (!id || graph.nodes.some((item) => item.id === id && item.id !== oldId)) return;
              setGraph((current) => ({ nodes: current.nodes.map((item) => item.id === oldId ? { ...item, id } : item), edges: current.edges.map((edge) => ({ ...edge, from: edge.from === oldId ? id : edge.from, to: edge.to === oldId ? id : edge.to })) }));
              setSelectedNode(id);
            }} /></label>
            <label>Python activity<select value={`${node.activity}@${node.version}`} onChange={(event) => { const activity = activities.find((item) => `${item.name}@${item.version}` === event.target.value); if (activity) updateNode(node.id, activity); }}>
              {activities.map((item) => <option key={`${item.name}@${item.version}`} value={`${item.name}@${item.version}`}>{item.name} v{item.version}</option>)}
            </select></label>
            <label>Inputs (JSON)<textarea className="flow-inputs-editor" value={inputsText} onChange={(event) => {
              const value = event.target.value;
              setInputsText(value);
              try { const parsed = JSON.parse(value); if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) updateNode(node.id, { inputs: parsed }); }
              catch { /* Keep incomplete JSON in the editor. */ }
            }} /></label>
            <p className="flow-hint">Use {`{"ref":"inputs.name"}`} for a run input or {`{"ref":"step1"}`} for an earlier result.</p>
          </> : <p className="flow-hint">Select an activity on the canvas.</p>}
          <div className="flow-inspector-footer">
            <button className="flow-icon-button" type="button" aria-label="Test published flow" title="Test published flow" disabled={!selected?.published_version || busy} onClick={() => selected && openRun(selected)}><Play size={16} /></button>
            {selectedId && <p className="flow-hint">POST /api/webhooks/flows/{selectedId}</p>}
          </div>
        </aside>
      </div>
    </> : <>
      <header className="flow-page-header">
        <div><h1>Flow designer</h1><p>Design, publish, and run your flows.</p></div>
        <div className="flow-toolbar-actions">
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
          onClick={() => setSelectedLibraryId(definition.id)} onDoubleClick={() => openEditor(definition)}
          onKeyDown={(event) => { if (event.target === event.currentTarget && event.key === "Enter") openEditor(definition); }}>
          <td><span className="flow-name-link"><span className="flow-row-icon"><Workflow size={16} /></span><span>{definition.name}</span></span></td>
          <td>{definition.draft.nodes.length}</td>
          <td><span className={`flow-status ${definition.published_version ? "published" : "draft"}`}>{definition.published_version ? `Published v${definition.published_version}` : "Draft"}</span></td>
          <td className="flow-date">{dateLabel(definition.updated_at)}</td>
          <td>{actions(definition)}</td>
        </tr>)}</tbody>
      </table></div> : <div className="flow-icon-grid">{visible.map((definition) => <article className={`flow-tile ${selectedLibraryId === definition.id ? "flow-library-selected" : ""}`} key={definition.id}
        tabIndex={0} aria-selected={selectedLibraryId === definition.id} title="Double-click to edit"
        onClick={() => setSelectedLibraryId(definition.id)} onDoubleClick={() => openEditor(definition)}
        onKeyDown={(event) => { if (event.target === event.currentTarget && event.key === "Enter") openEditor(definition); }}>
        <div className="flow-tile-top"><span className="flow-tile-icon"><Workflow size={20} /></span>{actions(definition)}</div>
        <span className="flow-tile-name">{definition.name}</span>
        <div className="flow-tile-meta"><span>{definition.draft.nodes.length} activities</span><span className={`flow-status ${definition.published_version ? "published" : "draft"}`}>{definition.published_version ? `Published v${definition.published_version}` : "Draft"}</span></div>
        <small>Updated {dateLabel(definition.updated_at)}</small>
      </article>)}</div>}
      {visible.length === 0 && <div className="flow-library-empty"><Workflow size={28} /><strong>{search ? "No matching flows" : "No flows yet"}</strong><span>{search ? "Try another search." : "Create your first flow."}</span></div>}
    </>}

    {message && <div className="flow-message" role="status">{message}<button type="button" aria-label="Dismiss message" onClick={() => setMessage("")}><X size={14} /></button></div>}

    {creating && <div className="flow-modal-backdrop" onClick={() => setCreating(false)}><div className="flow-modal" role="dialog" aria-modal="true" aria-label="Create flow" onClick={(event) => event.stopPropagation()}>
      <div className="flow-modal-heading"><h2>Create flow</h2><button className="flow-icon-button" aria-label="Close" onClick={() => setCreating(false)}><X size={17} /></button></div>
      <label>Flow name<input autoFocus placeholder="e.g. Daily report" value={newName} onChange={(event) => setNewName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void create(); }} /></label>
      {message && <p className="flow-modal-error" role="alert">{message}</p>}
      <div className="flow-modal-actions"><button className="flow-icon-button" aria-label="Cancel" title="Cancel" onClick={() => setCreating(false)}><X size={17} /></button><button className="flow-icon-button flow-icon-primary" aria-label="Create flow" title="Create flow" disabled={busy} onClick={create}><Check size={18} /></button></div>
    </div></div>}

    {runTarget && <div className="flow-modal-backdrop" onClick={() => setRunTarget(null)}><div className="flow-modal" role="dialog" aria-modal="true" aria-label={`Run ${runTarget.name}`} onClick={(event) => event.stopPropagation()}>
      <div className="flow-modal-heading"><h2>Run {runTarget.name}</h2><button className="flow-icon-button" aria-label="Close" onClick={() => setRunTarget(null)}><X size={17} /></button></div>
      <label>Inputs (JSON)<textarea value={runInputs} onChange={(event) => setRunInputs(event.target.value)} /></label>
      {message && <p className="flow-modal-error" role="alert">{message}</p>}
      <div className="flow-modal-actions"><button className="flow-icon-button" aria-label="Cancel" title="Cancel" onClick={() => setRunTarget(null)}><X size={17} /></button><button className="flow-icon-button flow-icon-primary" aria-label="Run flow" title="Run flow" disabled={busy} onClick={run}><Play size={17} /></button></div>
    </div></div>}
  </div>;
}
