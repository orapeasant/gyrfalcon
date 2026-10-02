import React, { useEffect, useState } from "react";
import { Inbox, RefreshCw } from "lucide-react";
import { api } from "../lib/api";

type Notice = { id: string; flow_run_id: string; node_id: string; channel: string; subject?: string; content?: string; body_html?: string; message_kind?: string; transients: string[]; created_at: number };
export function FlowTasksPage() {
  const [items,setItems]=useState<Notice[]>([]); const [values,setValues]=useState<Record<string,string>>({}); const [replies,setReplies]=useState<Record<string,string>>({}); const [busy,setBusy]=useState<string|null>(null);
  const load=async()=>{try{const d=await api.getFlowInbox();setItems(d.notifications||[]);}catch{setItems([]);}};
  useEffect(()=>{load();const id=setInterval(load,15000);return()=>clearInterval(id);},[]);
  async function respond(item:Notice){const transient=values[item.id]||item.transients[0];if(!transient){alert("This notification has no response transient.");return;}setBusy(item.id);try{await api.respondToVisualRun(item.flow_run_id,transient,replies[item.id]??"");await load();}catch(e:any){alert(e.message||"Could not respond");}finally{setBusy(null);}}
  return <div style={{padding:20,maxWidth:850}}><header style={{display:"flex",alignItems:"center",gap:10,marginBottom:16}}><h1 style={{fontSize:16,margin:0}}>My Tasks</h1><span>{items.length} waiting</span><button onClick={load} style={{marginLeft:"auto",display:"flex",gap:5,alignItems:"center"}}><RefreshCw size={13}/> Refresh</button></header>
    {!items.length?<div style={{padding:28,textAlign:"center",color:"var(--fg-muted)"}}><Inbox size={28}/><div>Nothing waiting on you right now.</div></div>:items.map(item=><article key={item.id} style={{padding:14,marginBottom:10,border:"1px solid var(--border)",borderRadius:8,background:"var(--card)"}}>
      <div style={{fontWeight:700}}>{item.subject||"Flow notification"}</div><div style={{fontSize:12,color:"var(--fg-muted)",margin:"5px 0"}}>Run {item.flow_run_id.slice(0,12)} · {item.node_id} · {item.channel}</div><div style={{whiteSpace:"pre-wrap",margin:"8px 0"}}>{item.content||""}</div>
      <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}><label>Outcome</label><select value={values[item.id]||item.transients[0]||""} onChange={e=>setValues(old=>({...old,[item.id]:e.target.value}))}>{item.transients.map(t=><option key={t}>{t}</option>)}</select><label>Reply</label><input value={replies[item.id]||""} onChange={e=>setReplies(old=>({...old,[item.id]:e.target.value}))} /><button disabled={busy===item.id||!item.transients.length} onClick={()=>respond(item)}>Respond</button></div>
    </article>)}
  </div>;
}
