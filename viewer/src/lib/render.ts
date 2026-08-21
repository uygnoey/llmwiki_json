import type { WikiPage } from "../types";

const esc = (value: unknown) => String(value ?? "").replace(
  /[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char] ?? char,
);

const links = (value: unknown) => esc(value).replace(
  /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]/g,
  (_match, target: string, alias?: string) => `<button class="wiki-inline-link" data-target="${target}">${alias || target}</button>`,
);

export function pageMarkdown(page: WikiPage): string {
  const frontmatter = [
    "---",
    `type: ${page.type}`,
    `created: ${page.created}`,
    `updated: ${page.updated}`,
    `tags: [${page.tags.join(", ")}]`,
    `projects: [${page.projects.join(", ")}]`,
    `sources: [${page.sources.join(", ")}]`,
    "---",
  ].join("\n");
  const body = page.block_order.map((id) => page.blocks[id].source_text).join("\n\n");
  return `${frontmatter}\n\n${body}\n`;
}

export function pageHtml(page: WikiPage): string {
  return page.block_order.map((id) => {
    const block = page.blocks[id]; const data = block.data;
    if (block.kind === "heading") { const level = Math.min(6, Math.max(1, Number(data.level ?? 2))); return `<h${level}>${links(data.text)}</h${level}>`; }
    if (["paragraph", "markdown", "raw"].includes(block.kind)) return `<p>${links(data.text ?? block.source_text).replace(/\n/g, "<br>")}</p>`;
    if (block.kind === "code") return `<pre><code>${esc(data.text)}</code></pre>`;
    if (block.kind === "table") return `<pre class="raw-structure">${esc(block.source_text)}</pre>`;
    if (block.kind === "list") return `<pre class="raw-structure">${esc(data.text ?? block.source_text)}</pre>`;
    if (["quote", "conflict", "current"].includes(block.kind)) return `<blockquote class="${block.kind}">${links(data.text)}</blockquote>`;
    return block.kind === "thematic_break" ? "<hr>" : `<pre>${esc(block.source_text)}</pre>`;
  }).join("");
}
