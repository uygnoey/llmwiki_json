import { Filter, Monitor, Moon, Search, Sun, X } from "lucide-react";
import { hash } from "../lib/colors";
import type { ThemeChoice } from "../lib/theme";
import type { ColorBy, GraphNode, GroupConfig } from "../types";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Input } from "./ui/input";
import { ScrollArea } from "./ui/scroll-area";
import { Separator } from "./ui/separator";
import { Switch } from "./ui/switch";
import { ToggleGroup, ToggleGroupItem } from "./ui/toggle-group";

interface Props {
  open: boolean; setOpen: (value: boolean) => void; query: string; setQuery: (value: string) => void;
  colorBy: ColorBy; setColorBy: (value: ColorBy) => void; nodes: GraphNode[]; visibleIds: Set<string>;
  enabledGroups: Set<string>; toggleGroup: (group: string) => void; groups: GroupConfig;
  showOrphans: boolean; setShowOrphans: (value: boolean) => void; conflictsOnly: boolean; setConflictsOnly: (value: boolean) => void;
  showColors: boolean; setShowColors: (value: boolean) => void; onSelect: (id: string) => void;
  themeChoice: ThemeChoice; setThemeChoice: (value: ThemeChoice) => void;
}

const THEME_OPTIONS: {value: ThemeChoice; label: string; Icon: typeof Sun}[] = [
  {value: "light", label: "라이트", Icon: Sun},
  {value: "dark", label: "다크", Icon: Moon},
  {value: "system", label: "시스템 설정 따름", Icon: Monitor},
];

/** 참조 콘솔과 같은 3상태 테마 스위치 — 라이트 / 다크 / 시스템. */
function ThemeSwitch({value, onChange}: {value: ThemeChoice; onChange: (next: ThemeChoice) => void}) {
  return <div className="theme-switch" role="radiogroup" aria-label="테마">
    {THEME_OPTIONS.map(({value: option, label, Icon}) => (
      <button key={option} type="button" role="radio" aria-checked={value === option} aria-label={label} title={label} onClick={() => onChange(option)}><Icon size={12}/></button>
    ))}
  </div>;
}

export default function Sidebar(props: Props) {
  const {open, setOpen, query, setQuery, colorBy, setColorBy, nodes, visibleIds, enabledGroups, toggleGroup, groups, showOrphans, setShowOrphans, conflictsOnly, setConflictsOnly, showColors, setShowColors, onSelect, themeChoice, setThemeChoice} = props;
  const groupValues = colorBy === "project" ? Object.keys(groups.project) : colorBy === "type" ? [...new Set(nodes.map((n) => n.type))].sort() : [...new Set(nodes.flatMap((n) => n.tags.length ? n.tags : ["untagged"]))].sort();
  const matches = query.trim() ? nodes.filter((node) => `${node.label} ${node.slug} ${node.summary} ${node.tags.join(" ")}`.toLowerCase().includes(query.toLowerCase())).slice(0, 8) : [];
  const color = (value: string) => colorBy === "project" ? groups.project[value]?.color : colorBy === "type" ? groups.type[value] : groups.tag_palette[hash(value) % groups.tag_palette.length];
  return <aside className={`sidebar ${open ? "is-open" : ""}`} aria-label="그래프 필터">
    <div className="sidebar-head"><div><strong>llmwiki_json</strong><span>Knowledge graph</span></div><div className="sidebar-head-actions"><ThemeSwitch value={themeChoice} onChange={setThemeChoice}/><Button size="icon" variant="ghost" className="mobile-only" onClick={() => setOpen(false)} aria-label="필터 닫기"><X/></Button></div></div>
    <label className="search-box"><Search size={16}/><Input value={query} onChange={(event)=>setQuery(event.target.value)} placeholder="페이지, 태그, 개념 검색" aria-label="그래프 검색"/><kbd>/</kbd></label>
    {matches.length > 0 && <div className="search-results">{matches.map((node)=><Button variant="ghost" key={node.id} onClick={()=>{setQuery("");onSelect(node.id)}}><span>{node.label}</span><small>{node.type}</small></Button>)}</div>}
    <section className="control-section"><h2><span><Filter size={13}/> 색상 기준</span></h2>
      <ToggleGroup className="segmented" type="single" variant="outline" spacing={0} value={colorBy} onValueChange={(value)=>value&&setColorBy(value as ColorBy)} aria-label="노드 색상 기준">
        <ToggleGroupItem value="project">프로젝트</ToggleGroupItem><ToggleGroupItem value="type">타입</ToggleGroupItem><ToggleGroupItem value="tag">태그</ToggleGroupItem>
      </ToggleGroup>
    </section>
    <section className="control-section grow"><h2><span>그룹</span><small>{visibleIds.size} / {nodes.length}</small></h2><ScrollArea className="group-list">{groupValues.map((value)=>{
      const count=nodes.filter((node)=>colorBy==="project"?node.group===value:colorBy==="type"?node.type===value:(node.tags.length?node.tags.includes(value):value==="untagged")).length;
      if(colorBy==="tag") return <div className="legend-row" key={value}><i style={{background:showColors?color(value):"#7e8798"}}/><span>#{value}</span><small>{count}</small></div>;
      return <label key={value}><Checkbox checked={enabledGroups.has(value)} onCheckedChange={()=>toggleGroup(value)} aria-label={`${value} 그룹 표시`}/><i style={{background:showColors?color(value):"#7e8798"}}/><span>{colorBy==="project"?(groups.project[value]?.label??value):value}</span><small>{count}</small></label>;
    })}</ScrollArea></section>
    <Separator className="sidebar-divider"/>
    <section className="control-section display-options"><h2>표시</h2>
      <label htmlFor="show-orphans"><span>고아 노드</span><Switch size="sm" id="show-orphans" checked={showOrphans} onCheckedChange={setShowOrphans}/></label>
      <label htmlFor="conflicts-only"><span>상충만 보기</span><Switch size="sm" id="conflicts-only" checked={conflictsOnly} onCheckedChange={setConflictsOnly}/></label>
      <label htmlFor="show-colors"><span>색상 표시</span><Switch size="sm" id="show-colors" checked={showColors} onCheckedChange={setShowColors}/></label>
    </section>
  </aside>;
}
