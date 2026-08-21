export type ColorBy = "project" | "type" | "tag";

export interface GraphNode {
  id: string;
  slug: string;
  label: string;
  type: string;
  created: string;
  updated: string;
  projects: string[];
  tags: string[];
  group: string;
  summary: string;
  incoming: number;
  outgoing: number;
  degree: number;
  unresolved_conflicts: number;
  orphan: boolean;
  data_url: string;
  x: number;
  y: number;
}

export interface GraphEdge { id: string; source: string; target: string; kind: string }
export interface ProjectGroup { label: string; color: string; match: string[] }
export interface GroupConfig {
  project: Record<string, ProjectGroup>;
  type: Record<string, string>;
  tag_palette: string[];
}
export interface GraphPayload { schema_version: string; nodes: GraphNode[]; edges: GraphEdge[]; groups: GroupConfig }
export interface GraphSettings {
  nodeScale: number;
  linkThickness: number;
  centerForce: number;
  repelForce: number;
  linkStrength: number;
  linkDistance: number;
}

export interface GraphCanvasHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  reset: () => void;
  replay: () => void;
}
export interface WikiBlock { id: string; kind: string; data: Record<string, unknown>; refs: string[]; source_text: string; fingerprint: string; resolution?: { status: string; current?: string; decided_at?: string } }
export interface WikiPage {
  schema_version: string; id: string; slug: string; title: string; type: string; created: string; updated: string;
  tags: string[]; projects: string[]; sources: string[]; raw_ref?: string | null; summary?: string;
  blocks: Record<string, WikiBlock>; block_order: string[]; links: Array<{target: string; label?: string; anchor?: string; kind: string; block_id?: string}>;
  history: Array<Record<string, string>>;
}
export interface MapPayload { pages: Record<string, {data_url: string; sha256: string; pointer: string; source: string}>; blocks: Record<string, unknown> }
export interface Stats { pages: number; blocks: number; edges: number; unresolved_conflicts: number }
