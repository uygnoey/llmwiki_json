import { Activity, Focus, Menu, Minus, Play, Plus, Search, Settings2, SlidersHorizontal } from "lucide-react";
import { lazy, Suspense, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import GraphSettingsPanel from "./components/GraphSettingsPanel";
import Inspector from "./components/Inspector";
import Sidebar from "./components/Sidebar";
import { Button } from "./components/ui/button";
import { Skeleton } from "./components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "./components/ui/tooltip";
import { primaryTag } from "./lib/colors";
import { useTheme } from "./lib/theme";
import type { ColorBy, GraphCanvasHandle, GraphPayload, GraphSettings, Stats, WikiPage } from "./types";

const Graph3DCanvas = lazy(() => import("./components/Graph3DCanvas"));
const EMPTY: GraphPayload = {schema_version:"1.0",nodes:[],edges:[],groups:{project:{},type:{},tag_palette:[]}};
const DEFAULT_GRAPH_SETTINGS: GraphSettings = {nodeScale:0.5,linkThickness:0.15,centerForce:.52,repelForce:10,linkStrength:1,linkDistance:250};
/** Data failures surface in development only — production ships no console output. */
const reportDataError=(error:unknown)=>{if(import.meta.env.DEV)console.error("[llmwiki] data load failed",error)};

interface ToolButtonProps {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}

function ToolButton({label, onClick, children}: ToolButtonProps) {
  return <Tooltip><TooltipTrigger asChild><Button aria-label={label} size="icon-sm" variant="ghost" onClick={onClick}>{children}</Button></TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>;
}

export default function App() {
  const [data,setData]=useState<GraphPayload>(EMPTY);
  const [stats,setStats]=useState<Stats>({pages:0,blocks:0,edges:0,unresolved_conflicts:0});
  const [query,setQuery]=useState("");
  const [colorBy,setColorBy]=useState<ColorBy>("project");
  const [enabledGroups,setEnabledGroups]=useState<Set<string>>(new Set());
  const [showOrphans,setShowOrphans]=useState(true);
  const [conflictsOnly,setConflictsOnly]=useState(false);
  const [showColors,setShowColors]=useState(true);
  const [settingsOpen,setSettingsOpen]=useState(false);
  const [graphSettings,setGraphSettings]=useState<GraphSettings>(DEFAULT_GRAPH_SETTINGS);
  const [selectedId,setSelectedId]=useState<string|null>(new URLSearchParams(location.search).get("node"));
  const [page,setPage]=useState<WikiPage|null>(null);
  const [loadingPage,setLoadingPage]=useState(false);
  const [sidebarOpen,setSidebarOpen]=useState(false);
  const {choice:themeChoice,resolved:theme,setChoice:setThemeChoice}=useTheme();
  const graphRef=useRef<GraphCanvasHandle>(null);
  const deferredGraphSettings=useDeferredValue(graphSettings);

  const loadData=useCallback(async()=>{
    const version=Date.now();
    const [graph,nextStats]=await Promise.all([
      fetch(`/data/graph.json?v=${version}`,{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`graph ${r.status}`);return r.json()}),
      fetch(`/data/stats.json?v=${version}`,{cache:"no-store"}).then(r=>{if(!r.ok)throw new Error(`stats ${r.status}`);return r.json()}),
    ]);
    setData(graph);setStats(nextStats);
  },[]);
  useEffect(()=>{
    void loadData().catch(reportDataError);
    if(import.meta.hot){
      const refresh=()=>void loadData().catch(reportDataError);
      import.meta.hot.on("llmwiki:data",refresh);
      return()=>import.meta.hot?.off("llmwiki:data",refresh);
    }
  },[loadData]);
  useEffect(()=>{
    const groups=colorBy==="project"?Object.keys(data.groups.project):colorBy==="type"?[...new Set(data.nodes.map(n=>n.type))]:[...new Set(data.nodes.flatMap(n=>n.tags.length?n.tags:["untagged"]))];
    setEnabledGroups(new Set(groups));
  },[colorBy,data]);
  const selectedNode=useMemo(()=>data.nodes.find(n=>n.id===selectedId)??null,[data,selectedId]);
  useEffect(()=>{
    const url=new URL(location.href); if(selectedId)url.searchParams.set("node",selectedId);else url.searchParams.delete("node"); history.replaceState(null,"",url);
    if(!selectedNode){setPage(null);return;} setLoadingPage(true);
    fetch(`/data/${selectedNode.data_url}`).then(r=>r.json()).then(setPage).finally(()=>setLoadingPage(false));
  },[selectedId,selectedNode]);
  useEffect(()=>{
    const handler=(event:KeyboardEvent)=>{if(event.key==="/"&&!(event.target instanceof HTMLInputElement)){event.preventDefault();setSidebarOpen(true);setTimeout(()=>document.querySelector<HTMLInputElement>(".search-box input")?.focus(),40)}if(event.key==="Escape"){setSelectedId(null);setSidebarOpen(false)}};
    addEventListener("keydown",handler);return()=>removeEventListener("keydown",handler);
  },[]);
  const visibleIds=useMemo(()=>new Set(data.nodes.filter(node=>{
    const group=colorBy==="project"?node.group:colorBy==="type"?node.type:primaryTag(node);
    const groupEnabled=colorBy==="tag"?true:enabledGroups.has(group);
    if(!groupEnabled)return false;if(!showOrphans&&node.orphan)return false;if(conflictsOnly&&node.unresolved_conflicts===0)return false;
    if(query.trim()&&!`${node.label} ${node.slug} ${node.summary} ${node.tags.join(" ")} ${node.projects.join(" ")}`.toLowerCase().includes(query.toLowerCase()))return false;
    return true;
  }).map(n=>n.id)),[data,colorBy,enabledGroups,showOrphans,conflictsOnly,query]);
  const select=useCallback((id:string|null)=>{setSelectedId(id);if(id)setSidebarOpen(false)},[]);
  const navigateSlug=(slug:string)=>{const node=data.nodes.find(n=>n.slug===slug);if(node)select(node.id)};
  const toggleGroup=(group:string)=>setEnabledGroups(current=>{const next=new Set(current);next.has(group)?next.delete(group):next.add(group);return next});
  const graphProps={data,visibleIds,selectedId,colorBy,showColors,theme,onSelect:select};

  return <main className={`app-shell${theme === "dark" ? " dark" : ""}${selectedNode ? " has-inspector" : ""}`}>
    <div className="mobile-topbar"><Button size="icon" variant="ghost" aria-label="그래프 필터 열기" onClick={()=>setSidebarOpen(true)}><Menu/></Button><strong>llmwiki_json</strong><Button size="icon" variant="ghost" aria-label="페이지 검색 열기" onClick={()=>{setSidebarOpen(true);setTimeout(()=>document.querySelector<HTMLInputElement>(".search-box input")?.focus(),30)}}><Search/></Button></div>
    <Sidebar open={sidebarOpen} setOpen={setSidebarOpen} query={query} setQuery={setQuery} colorBy={colorBy} setColorBy={setColorBy} nodes={data.nodes} visibleIds={visibleIds} enabledGroups={enabledGroups} toggleGroup={toggleGroup} groups={data.groups} showOrphans={showOrphans} setShowOrphans={setShowOrphans} conflictsOnly={conflictsOnly} setConflictsOnly={setConflictsOnly} showColors={showColors} setShowColors={setShowColors} onSelect={select} themeChoice={themeChoice} setThemeChoice={setThemeChoice}/>
    {sidebarOpen&&<button className="backdrop mobile-only" aria-label="필터 닫기" onClick={()=>setSidebarOpen(false)}/>} 
    <section className="graph-stage">
      <div className="canvas-heading"><div><span className="live-dot"/>GRAPH · 3D</div><strong>{visibleIds.size}</strong><span>of {stats.pages} pages</span></div>
      <div className="canvas-toolbar">
        <ToolButton label="확대" onClick={()=>graphRef.current?.zoomIn()}><Plus/></ToolButton>
        <ToolButton label="축소" onClick={()=>graphRef.current?.zoomOut()}><Minus/></ToolButton>
        <ToolButton label="전체 맞춤" onClick={()=>graphRef.current?.reset()}><Focus/></ToolButton>
        <i/>
        <ToolButton label="그래프 처음부터 재생" onClick={()=>graphRef.current?.replay()}><Play/></ToolButton>
        <ToolButton label="그래프 설정" onClick={()=>setSettingsOpen(value=>!value)}><Settings2/></ToolButton>
      </div>
      {data.nodes.length ? <Suspense fallback={<div className="graph-loading"><Skeleton className="h-2 w-40"/><span>그래프 엔진 불러오는 중…</span></div>}><Graph3DCanvas ref={graphRef} {...graphProps} settings={deferredGraphSettings}/></Suspense> : <div className="graph-loading"><Activity size={22}/><span>그래프 인덱스 불러오는 중…</span></div>}
      <GraphSettingsPanel open={settingsOpen} settings={graphSettings} onChange={setGraphSettings} onClose={()=>setSettingsOpen(false)} onReplay={()=>graphRef.current?.replay()} onReset={()=>setGraphSettings(DEFAULT_GRAPH_SETTINGS)}/>
      <div className="status-bar"><div><span>{stats.pages} nodes</span><span>{stats.edges} edges</span><span>{stats.blocks} blocks</span>{stats.unresolved_conflicts>0&&<span className="status-conflict">{stats.unresolved_conflicts} unresolved</span>}</div><div className="desktop-only"><kbd>/</kbd> Search <kbd>Esc</kbd> Clear · drag to orbit · wheel to zoom</div><Button size="xs" variant="ghost" className="mobile-filter-button mobile-only" onClick={()=>setSidebarOpen(true)}><SlidersHorizontal data-icon="inline-start"/>필터</Button></div>
    </section>
    <Inspector node={selectedNode} page={page} loading={loadingPage} onClose={()=>select(null)} onNavigate={navigateSlug}/>
  </main>;
}
