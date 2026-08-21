import { ArrowLeft, Braces, ExternalLink, FileText, Link2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { pageHtml, pageMarkdown } from "../lib/render";
import type { GraphNode, WikiPage } from "../types";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { ScrollArea } from "./ui/scroll-area";
import { Skeleton } from "./ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "./ui/tabs";

interface Props { node: GraphNode | null; page: WikiPage | null; loading: boolean; onClose: () => void; onNavigate: (slug: string) => void }
type Tab = "rendered" | "markdown" | "json";

export default function Inspector({node,page,loading,onClose,onNavigate}: Props) {
  const [tab,setTab]=useState<Tab>("rendered");
  useEffect(()=>setTab("rendered"),[node?.id]);
  if (!node) return null;
  return <aside className="inspector" aria-label="선택한 페이지 상세">
    <header><Button size="icon" variant="ghost" className="mobile-only" aria-label="그래프로 돌아가기" onClick={onClose}><ArrowLeft/></Button><div><small>{node.type}</small><h1>{node.label}</h1></div><Button size="icon" variant="ghost" onClick={onClose} aria-label="상세 닫기"><X/></Button></header>
    <div className="inspector-meta">
      <div><span>PROJECT</span><strong>{node.projects.join(" · ") || "미분류"}</strong></div>
      <div><span>LINKS</span><strong>{node.incoming} in · {node.outgoing} out</strong></div>
      <div><span>TYPE</span><strong>{node.type}</strong></div>
      <div><span>UPDATED</span><strong>{node.updated}</strong></div>
    </div>
    <div className="tag-row">{node.tags.length ? node.tags.map((tag)=><Badge variant="secondary" key={tag}>#{tag}</Badge>) : <Badge variant="secondary">태그 없음</Badge>}</div>
    {node.unresolved_conflicts>0 && <Alert variant="destructive" className="conflict-banner"><TriangleAlert/><AlertTitle>미판정 상충 {node.unresolved_conflicts}건</AlertTitle><AlertDescription>결론을 선택하지 않고 양쪽 주장을 유지합니다.</AlertDescription></Alert>}
    <Tabs value={tab} onValueChange={(value)=>setTab(value as Tab)}><TabsList variant="line" className="inspector-tabs">
      <TabsTrigger value="rendered"><FileText data-icon="inline-start"/>보기</TabsTrigger><TabsTrigger value="markdown"><Link2 data-icon="inline-start"/>MD</TabsTrigger><TabsTrigger value="json"><Braces data-icon="inline-start"/>JSON</TabsTrigger>
    </TabsList></Tabs>
    <ScrollArea className="inspector-content">
      {loading && <div className="loading-state"><Skeleton className="mb-3 h-4 w-44"/><Skeleton className="h-24 w-full"/></div>}
      {!loading && page && tab==="rendered" && <article className="rendered-page" onClick={(event)=>{const target=(event.target as HTMLElement).closest<HTMLElement>("[data-target]");if(target)onNavigate(target.dataset.target!)}} dangerouslySetInnerHTML={{__html:pageHtml(page)}}/>}
      {!loading && page && tab==="markdown" && <pre>{pageMarkdown(page)}</pre>}
      {!loading && page && tab==="json" && <pre>{JSON.stringify(page,null,2)}</pre>}
    </ScrollArea>
    <footer><span>정본 · {node.id}</span><Button size="xs" variant="ghost" onClick={()=>setTab("json")}>원 데이터 <ExternalLink data-icon="inline-end"/></Button></footer>
  </aside>;
}
