import type { ColorBy, GraphNode, GroupConfig } from "../types";

export function hash(input: string): number {
  let value = 0;
  for (let index = 0; index < input.length; index += 1) value = ((value << 5) - value + input.charCodeAt(index)) | 0;
  return Math.abs(value);
}

export function primaryTag(node: GraphNode): string { return node.tags[0] ?? "untagged"; }

export function nodeColor(node: GraphNode, colorBy: ColorBy, groups: GroupConfig, showColors = true): string {
  if (!showColors) return "#7e8798";
  if (colorBy === "project") return groups.project[node.group]?.color ?? groups.project.ungrouped.color;
  if (colorBy === "type") return groups.type[node.type] ?? "#7e8798";
  return groups.tag_palette[hash(primaryTag(node)) % groups.tag_palette.length];
}
