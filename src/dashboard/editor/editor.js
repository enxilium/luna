// Luna's Markdown editor: one CodeMirror 6 view over plain Markdown with two modes, switched on purpose (⌘E), never by
// where the cursor is. Read renders everything (maths with KaTeX, images, code blocks, callouts, columns, tables) and isn't
// editable, though task boxes still click. Edit is a live preview: everything renders but the area the cursor is in (its
// line, or a whole block, nesting like capsules), which shows its source: blocks keep their frames, display maths keeps
// its rendering underneath, and on the line being edited, emphasis marks and inline maths render once the cursor leaves
// their span (maths previews in a floating tip while you're in it). Notes and task descriptions share it; only their headers differ.

import { EditorState, EditorSelection, StateField, StateEffect, Prec } from "@codemirror/state";
import { EditorView, Decoration, ViewPlugin, WidgetType, keymap, placeholder as placeholderExt, drawSelection, showTooltip } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { markdown, markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
import { syntaxTree, syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { autocompletion, completionKeymap, startCompletion, snippetCompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { tags as t, Tag } from "@lezer/highlight";
import katex from "katex";
import { Marked } from "marked";
import { language, codeBlockHtml, classHighlighter } from "./code.js";

// Rendered blocks' code fences get the same highlighting and frame as the editor's own, and `__text__` is underline (as
// in Discord), not a second spelling of bold.
const marked = new Marked({ gfm: true, breaks: false, renderer: {
  code: ({ text, lang }) => codeBlockHtml(text, lang),
  strong(token) { const inner = this.parser.parseInline(token.tokens); return token.raw.startsWith("__") ? `<u>${inner}</u>` : `<strong>${inner}</strong>`; },
} });

// Underline: `__text__`, as in Discord. Markdown reads it as bold, so the editor parses it first; three or more
// underscores are left to emphasis, and like `_` it never opens or closes inside a word.
const underlineTag = Tag.define();
const UnderlineDelim = { resolve: "Underline", mark: "UnderlineMark" };
const Underline = {
  defineNodes: [{ name: "Underline", style: { "Underline/...": underlineTag } }, { name: "UnderlineMark", style: t.processingInstruction }],
  parseInline: [{
    name: "Underline",
    before: "Emphasis",
    parse(cx, next, pos) {
      if (next !== 95 || cx.char(pos + 1) !== 95 || cx.char(pos + 2) === 95 || cx.char(pos - 1) === 95) return -1;
      const before = cx.slice(pos - 1, pos), after = cx.slice(pos + 2, pos + 3);
      const sBefore = /\s|^$/.test(before), sAfter = /\s|^$/.test(after);
      const pBefore = /[\p{P}\p{S}]/u.test(before), pAfter = /[\p{P}\p{S}]/u.test(after);
      const left = !sAfter && (!pAfter || sBefore || pBefore), right = !sBefore && (!pBefore || sAfter || pAfter);
      return cx.addDelimiter(UnderlineDelim, pos, pos + 2, left && (!right || pBefore), right && (!left || pAfter));
    },
  }],
};

// Inline maths: $x^2$. A `$` next to another `$`, after a backslash, or with space inside its edges isn't one.
// `$$…$$` inside a line is display maths in its own place (the way rendered callouts and columns read it); on a line of
// its own it's a block (the block field), and an opening `$$` with nothing after it on the line is left alone.
const mathMarks = (doc, node) => (doc.sliceString(node.from, node.from + 2) === "$$" ? 2 : 1);
const InlineMath = {
  defineNodes: [{ name: "InlineMath" }, { name: "InlineMathMark", style: t.processingInstruction }],
  parseInline: [{
    name: "InlineMath",
    before: "Emphasis",
    parse(cx, next, pos) {
      if (next !== 36 || cx.char(pos - 1) === 36 || cx.char(pos - 1) === 92) return -1;
      if (cx.char(pos + 1) === 36) {
        for (let end = pos + 2; end < cx.end - 1; end++) {
          const c = cx.char(end);
          if (c === 10) return -1;
          if (c === 36 && cx.char(end + 1) === 36 && cx.char(end - 1) !== 92) {
            if (end === pos + 2) return -1;
            return cx.addElement(cx.elt("InlineMath", pos, end + 2, [cx.elt("InlineMathMark", pos, pos + 2), cx.elt("InlineMathMark", end, end + 2)]));
          }
        }
        return -1;
      }
      if (cx.char(pos + 1) === 32) return -1;
      for (let end = pos + 1; end < cx.end; end++) {
        const c = cx.char(end);
        if (c === 10) return -1;
        if (c === 36 && cx.char(end - 1) !== 92) {
          if (end === pos + 1 || cx.char(end - 1) === 32 || cx.char(end + 1) === 36) return -1;
          return cx.addElement(cx.elt("InlineMath", pos, end + 1, [cx.elt("InlineMathMark", pos, pos + 1), cx.elt("InlineMathMark", end, end + 1)]));
        }
      }
      return -1;
    },
  }],
};

const tex = (source, displayMode) => {
  try { return katex.renderToString(source, { displayMode, throwOnError: false, output: "html", strict: "ignore" }); }
  catch { return `<span class="md-math-error">${escape(source)}</span>`; }
};
const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// Rendered Markdown for blocks shown whole (the columns): maths first, then Markdown, then strip anything active.
function render(source, resolve) {
  // Code keeps its dollar signs: set fences and inline code aside until the maths is done.
  const code = [];
  const keep = (m) => `\u0000${code.push(m) - 1}\u0000`;
  const withMath = source
    .replace(/^(\s*)(```|~~~)[^\n]*\n[\s\S]*?\n\s*\2[ \t]*$/gm, keep)
    .replace(/`[^`\n]+`/g, keep)
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, s) => tex(s.trim(), true))
    .replace(/(^|[^\\$])\$([^\s$](?:[^$\n]*[^\s$])?)\$(?!\$)/g, (_, pre, s) => pre + tex(s, false))
    .replace(/(!?\[[^\]]*\]\()([^)\s]+)/g, (m, head, href) => (/^[a-z]+:/i.test(href) ? m : head + resolve(href)))
    .replace(/\u0000(\d+)\u0000/g, (_, i) => code[+i]);
  const doc = new DOMParser().parseFromString(marked.parse(withMath), "text/html");
  doc.querySelectorAll("script, iframe, object, embed, style").forEach((n) => n.remove());
  doc.querySelectorAll("*").forEach((n) => [...n.attributes].forEach((a) => {
    if (/^on/i.test(a.name) || (/^(href|src)$/i.test(a.name) && /^\s*javascript:/i.test(a.value))) n.removeAttribute(a.name);
  }));
  return doc.body.innerHTML;
}

// A rendered block's body in pieces split at blank lines (never inside a fence or $$ … $$).
function chunks(text) {
  const lines = text.split("\n"), out = [];
  let start = -1, fence = null, math = false;
  const push = (end) => { while (end > start && !lines[end - 1].trim()) end--; out.push({ start, count: end - start }); start = -1; };
  lines.forEach((line, i) => {
    const t = line.trim(), open = FENCE.exec(line);
    if (start < 0 && !t) return;
    if (start < 0) start = i;
    if (fence) { if (open && open[1][0] === fence[0] && open[1].length >= fence.length && !open[2].trim()) fence = null; }
    else if (open) fence = open[1];
    else if (t === "$$") math = !math;
    else if (!t && !math) push(i);
  });
  if (start >= 0) push(lines.length);
  // `gap`: the blank lines between this piece and the one before; any number of them is one paragraph gap, as on the page.
  return out.map((c, i) => ({ ...c, gap: i ? c.start - out[i - 1].start - out[i - 1].count : 0, text: lines.slice(c.start, c.start + c.count).join("\n") }));
}

const renderChunks = (text, resolve) => chunks(text)
  .map((c) => `<div class="md-chunk${c.gap ? " has-gap" : ""}">${render(c.text, resolve)}</div>`).join("");

// Widgets ----------------------------------------------------------------------------------------------------------

// Off-screen blocks: CodeMirror counts a block widget it hasn't drawn as one line unless the widget says otherwise, so a
// long note grew as you scrolled through it and the scrollbar drifted. Blocks report the height they last drew at (by
// content), else a guess from their source.
const drawn = new Map();
function remember(key, el) {
  requestAnimationFrame(() => {
    if (!el.isConnected) return;
    if (drawn.size > 2000) drawn.clear();
    drawn.set(key, el.getBoundingClientRect().height);
  });
}
class BlockWidget extends WidgetType {
  get estimatedHeight() { return drawn.get(this.key) ?? this.guess; }
}
const rowsOf = (text) => (text ? text.split("\n").length : 0);

// Inline maths, or a display block. `preview` is the rendering Edit mode keeps under an equation's source. Inline maths
// written `$$…$$` mid-line is `wide`: display style in its own place.
class MathWidget extends BlockWidget {
  constructor(source, display, preview = false, wide = false) { super(); Object.assign(this, { source, display, preview, wide }); }
  eq(o) { return o.source === this.source && o.display === this.display && o.preview === this.preview && o.wide === this.wide; }
  get key() { return `math\u0000${this.source}`; }
  get guess() { return 40 + 30 * ((this.source.match(/\\\\/g) || []).length + 1); }
  get estimatedHeight() { return this.display ? super.estimatedHeight : -1; }
  toDOM(view) {
    const el = document.createElement(this.display ? "div" : "span");
    el.className = this.display ? `md-math md-math-block${this.preview ? " is-preview" : ""}` : `md-math${this.wide ? " is-wide" : ""}`;
    el.innerHTML = tex(this.source, this.display || this.wide);
    if (!this.display) {
      // Clicking rendered inline maths in Edit mode opens its source: the cursor goes just inside its opening `$`.
      el.addEventListener("mousedown", (e) => {
        if (!view || !editing(view.state)) return;
        e.preventDefault();
        view.focus();
        view.dispatch({ selection: { anchor: view.posAtDOM(el) + (this.wide ? 2 : 1) }, userEvent: "select.pointer" });
      });
      return el;
    }
    const block = spaced(el, this.preview ? "preview" : "math");
    remember(this.key, block);
    return block;
  }
  ignoreEvent() { return false; }
}

// An image in place (Read), or under the line that names it (Edit, `below`).
class ImageWidget extends WidgetType {
  constructor(src, alt, below = false) { super(); Object.assign(this, { src, alt, below }); }
  eq(o) { return o.src === this.src && o.alt === this.alt && o.below === this.below; }
  toDOM() {
    const fig = document.createElement(this.below ? "div" : "span");
    fig.className = this.below ? "md-image md-image-below" : "md-image";
    const img = document.createElement("img");
    img.src = this.src; img.alt = this.alt; img.loading = "lazy"; img.draggable = false;
    fig.appendChild(img);
    return fig;
  }
  ignoreEvent() { return false; }
}

class CheckWidget extends WidgetType {
  constructor(done, pos) { super(); this.done = done; this.pos = pos; }
  eq(o) { return o.done === this.done && o.pos === this.pos; }
  toDOM(view) {
    const box = document.createElement("span");
    box.className = `md-check${this.done ? " is-done" : ""}`;
    box.setAttribute("role", "checkbox");
    box.setAttribute("aria-checked", String(this.done));
    box.addEventListener("mousedown", (e) => {
      e.preventDefault();
      view.dispatch({ changes: { from: this.pos + 1, to: this.pos + 2, insert: this.done ? " " : "x" } });
    });
    return box;
  }
  ignoreEvent() { return true; }
}

class BulletWidget extends WidgetType {
  eq() { return true; }
  toDOM() { const b = document.createElement("span"); b.className = "md-bullet"; b.textContent = "•"; return b; }
}

class RuleWidget extends WidgetType {
  eq() { return true; }
  toDOM() { const hr = document.createElement("span"); hr.className = "md-rule"; return hr; }
}

// CodeMirror measures a block widget without its CSS margins, so a margin would shift every line below it and send
// clicks and ↑/↓ to the wrong line. Blocks get their spacing from padding on this margin-free wrapper instead.
function spaced(el, kind) {
  const wrap = document.createElement("div");
  wrap.className = `md-block md-block-${kind}`;
  wrap.appendChild(el);
  return wrap;
}

// A rendered code block's copy button. Nothing else in a rendered block takes clicks: Read mode isn't for editing.
function copyButton(el) {
  el.addEventListener("mousedown", (e) => {
    const copy = e.target.closest(".md-copy");
    if (!copy) return;
    e.preventDefault();
    const code = copy.closest(".md-pre").querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(() => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1400); }, () => {});
  });
}

// Columns arrive as arrays of lines, one per column.
class ColumnsWidget extends BlockWidget {
  constructor(columns, resolve) { super(); this.columns = columns; this.resolve = resolve; }
  eq(o) { return o.key === this.key; }
  get key() { return `columns\u0000${this.columns.map((c) => c.join("\n")).join("\u0001")}`; }
  get guess() { return 16 + 22 * Math.max(...this.columns.map((c) => c.length)); }
  toDOM() {
    const el = document.createElement("div");
    el.className = "md-columns";
    el.style.setProperty("--columns", String(this.columns.length));
    for (const lines of this.columns) {
      const col = document.createElement("div");
      col.className = "md-column md-rendered";
      col.innerHTML = renderChunks(lines.join("\n"), this.resolve);
      el.appendChild(col);
    }
    copyButton(el);
    const block = spaced(el, "columns");
    remember(this.key, block);
    return block;
  }
  ignoreEvent() { return false; }
}

// A boxed callout: "::: definition Title … :::", the container syntax the notes were written in. Its body is full Markdown,
// maths, lists and code included. The header names the kind before the title ("Definition  Bootstrap Aggregation"), so
// colour is never the only cue; a warning's ⚠️ says enough on its own. Unknown kinds read as notes under their own name.
export const CALLOUTS = {
  note: { label: "Note" }, tip: { label: "Tip" }, definition: { label: "Definition" }, theorem: { label: "Theorem" },
  example: { label: "Example" }, warning: { label: "⚠️", aria: "Warning" },
};
// "::: definition Theorem: Euler's Formula" is a theorem that predates the theorem kind: the title's own label wins.
const TITLED = /^(Theorem|Lemma|Corollary|Proposition|Proof|Example|Definition|Note|Tip)\s*:\s*/i;

export function calloutHead(type, title) {
  let kind = CALLOUTS[type] ? type : "note", label = CALLOUTS[type] ? CALLOUTS[type].label : type.charAt(0).toUpperCase() + type.slice(1);
  const named = TITLED.exec(title || "");
  if (named && kind !== "warning") {
    const word = named[1].toLowerCase();
    kind = CALLOUTS[word] ? word : "theorem";
    label = named[1].charAt(0).toUpperCase() + named[1].slice(1).toLowerCase();
    title = title.slice(named[0].length);
  }
  return { kind, label, aria: CALLOUTS[kind] && CALLOUTS[kind].aria, title: (title || "").trim() };
}

class CalloutWidget extends BlockWidget {
  constructor(type, title, body, resolve) { super(); Object.assign(this, { type, title, body, resolve }); }
  eq(o) { return o.type === this.type && o.title === this.title && o.body === this.body; }
  get key() { return `callout\u0000${this.type}\u0000${this.title}\u0000${this.body}`; }
  get guess() { return 62 + (this.body.trim() ? 8 + 22 * rowsOf(this.body) : 0); }
  toDOM() {
    const head = calloutHead(this.type, this.title);
    const el = document.createElement("div");
    el.className = `md-box md-box-${head.kind}`;
    const top = document.createElement("div");
    top.className = "md-box-title";
    const label = document.createElement("span");
    label.className = "md-box-label";
    label.textContent = head.label;
    if (head.aria) label.setAttribute("aria-label", head.aria);
    top.appendChild(label);
    if (head.title) {
      const name = document.createElement("span");
      name.className = "md-box-name md-rendered";
      name.innerHTML = render(head.title, this.resolve).replace(/^\s*<p>|<\/p>\s*$/g, "");
      top.appendChild(name);
    }
    el.appendChild(top);
    if (this.body.trim()) {
      const body = document.createElement("div");
      body.className = "md-box-body md-rendered";
      body.innerHTML = renderChunks(this.body, this.resolve);
      el.appendChild(body);
    }
    copyButton(el);
    const block = spaced(el, "callout");
    remember(this.key, block);
    return block;
  }
  ignoreEvent() { return false; }
}

// A fenced code block, rendered: the language, a copy button and the highlighted code.
class CodeWidget extends BlockWidget {
  constructor(info, code) { super(); this.info = info; this.code = code; }
  eq(o) { return o.info === this.info && o.code === this.code; }
  get key() { return `code\u0000${this.info}\u0000${this.code}`; }
  get guess() { return 73 + 20 * Math.max(rowsOf(this.code), 1); }
  toDOM() {
    const wrap = document.createElement("div");
    wrap.innerHTML = codeBlockHtml(this.code, this.info);
    const el = wrap.firstChild;
    copyButton(el);
    const block = spaced(el, "code");
    remember(this.key, block);
    return block;
  }
  ignoreEvent() { return false; }
}

// A table, rendered: header, hairline rows, cells' own inline Markdown and maths. Wide tables scroll sideways.
class TableWidget extends BlockWidget {
  constructor(text, resolve) { super(); this.text = text; this.resolve = resolve; }
  eq(o) { return o.text === this.text; }
  get key() { return `table\u0000${this.text}`; }
  get guess() { return 9 + 33 * (rowsOf(this.text) - 1); }
  toDOM() {
    const el = document.createElement("div");
    el.className = "md-table md-rendered";
    el.innerHTML = render(this.text, this.resolve);
    const block = spaced(el, "table");
    remember(this.key, block);
    return block;
  }
  ignoreEvent() { return false; }
}

// A component (```component … ```): HTML that Luna wrote for this note, run in a sandboxed frame. The frame has scripts
// but no origin of its own (no allow-same-origin), so it can't reach the dashboard's cookie, storage or API, and its CSP
// allows no network at all. It reports its height; nothing else crosses the boundary. `preview` sits under the source in Edit.
const THEME = ["--ink", "--ink-1", "--ink-2", "--ink-3", "--ink-4", "--hairline", "--surface", "--violet", "--lilac", "--on-lilac", "--coral",
  "--amber", "--green", "--font", "--mono", ...["iris", "orchid", "rose", "peach", "sand", "sage", "lagoon", "sky"].map((h) => `--hue-${h}`)];
const FRAME_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
  + "media-src data: blob:; form-action 'none'";
const MAX_FRAME = 1200;
function frameDoc(html) {
  const css = getComputedStyle(document.documentElement);
  const vars = THEME.map((name) => `${name}: ${css.getPropertyValue(name).trim()}`).join("; ");
  const size = `(() => { const post = () => parent.postMessage({ lunaComponent: "size",
    height: Math.ceil(document.documentElement.getBoundingClientRect().height) }, "*");
    new ResizeObserver(post).observe(document.documentElement); addEventListener("load", post); })();`;
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">
<style>:root { color-scheme: dark; ${vars} } html { height: auto !important; }
body { margin: 0; background: transparent; color: var(--ink-1); font: 13.5px/1.5 var(--font); overflow-x: auto; overflow-y: hidden; }</style>
<script>${size}</script></head><body>${html}</body></html>`;
}
addEventListener("message", (e) => {
  if (!e.data || e.data.lunaComponent !== "size") return;
  for (const frame of document.querySelectorAll("iframe.md-component-frame")) {
    if (frame.contentWindow !== e.source) continue;
    const height = Math.min(Math.max(Math.ceil(Number(e.data.height) || 0), 24), MAX_FRAME);
    if (frame.style.height !== `${height}px`) frame.style.height = `${height}px`;
    frame.__remember?.();
  }
});
class ComponentWidget extends BlockWidget {
  constructor(code, preview = false) { super(); this.code = code; this.preview = preview; }
  eq(o) { return o.code === this.code && o.preview === this.preview; }
  get key() { return `component\u0000${this.code}`; }
  get guess() { return 336; }
  toDOM() {
    const el = document.createElement("div");
    el.className = "md-component";
    const frame = document.createElement("iframe");
    frame.className = "md-component-frame";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = "Component";
    frame.style.height = `${drawn.has(this.key) ? Math.max(drawn.get(this.key) - 16, 24) : 320}px`;
    frame.srcdoc = frameDoc(this.code.replace(/^\s*<!--[\s\S]*?-->\s*/, ""));
    el.appendChild(frame);
    const block = spaced(el, this.preview ? "preview" : "component");
    frame.__remember = () => remember(this.key, block);
    return block;
  }
  ignoreEvent() { return true; }
}

// Tables in Edit mode stay a grid you type into (the one block that doesn't turn into source): click a cell, or ↑/↓ into
// the table, and type. A cell shows its Markdown while you're in it and renders when you leave; every keystroke rewrites
// the table's Markdown, padded so it lines up. Tab/⇧Tab and Enter move between cells (and add a row past the last),
// arrows leave at the edges, Backspace in an empty row deletes it, and right-click edits rows, columns and alignment.
const unescapeCell = (c) => c.replace(/\\\|/g, "|");
const escapeCell = (c) => c.replace(/\s*\n\s*/g, " ").replace(/(?<!\\)\|/g, "\\|").trim();
function tableModel(text) {
  const lines = text.split("\n"), aligns = cellsOf(lines[1]).map(alignOf);
  const rows = [lines[0], ...lines.slice(2)].map((l) => cellsOf(l).map(unescapeCell));
  return { aligns, rows: rows.map((r) => aligns.map((_, i) => r[i] ?? "")) };
}
const tableText = (model) => tableMarkdown(model.rows.map((r) => r.map(escapeCell)), model.aligns);
// A cell's inline Markdown, rendered; a leading list or heading mark stays text.
const cellHtml = (raw, resolve) => (raw.trim()
  ? render(raw.replace(/^(\s*)(\d+[.)]|[-*+#>])(\s)/, "$1\\$2$3"), resolve).replace(/^\s*<p>|<\/p>\s*$/g, "") : "");

class TableEditWidget extends WidgetType {
  constructor(text, resolve) { super(); this.text = text; this.resolve = resolve; }
  eq(o) { return o.text === this.text; }
  get estimatedHeight() { return drawn.get(`tablegrid\u0000${this.text}`) ?? 25 + 33 * (rowsOf(this.text) - 1); }
  toDOM(view) {
    const grid = document.createElement("div");
    grid.className = "md-table md-table-edit md-rendered";
    const block = spaced(grid, "table");
    block.__table = { widget: this, view, model: tableModel(this.text), grid };
    fillTable(block);
    grid.addEventListener("focusin", (e) => { const cell = e.target.closest("[data-r]"); if (cell) openCell(block, cell); });
    grid.addEventListener("focusout", (e) => { const cell = e.target.closest("[data-r]"); if (cell) closeCell(block, cell); });
    grid.addEventListener("input", (e) => {
      const cell = e.target.closest("[data-r]");
      if (!cell) return;
      block.__table.model.rows[+cell.dataset.r][+cell.dataset.c] = cell.textContent.replace(/\n/g, " ");
      commitTable(block);
    });
    grid.addEventListener("keydown", (e) => tableKey(block, e));
    grid.addEventListener("contextmenu", (e) => tableMenu(block, e));
    grid.addEventListener("mousedown", (e) => {
      const add = e.target.closest("[data-add]");
      if (!add) return;
      e.preventDefault();
      const { model } = block.__table;
      if (add.dataset.add === "row") reshapeTable(block, (m) => m.rows.push(m.aligns.map(() => "")), [model.rows.length, 0]);
      else reshapeTable(block, (m) => { m.aligns.push(""); m.rows.forEach((r) => r.push("")); }, [0, model.aligns.length]);
    });
    remember(`tablegrid\u0000${this.text}`, block);
    return block;
  }
  // A keystroke in a cell rewrites the table; the cell being typed in keeps its DOM (and caret), the others catch up.
  updateDOM(dom, view) {
    const t = dom.__table;
    if (!t) return false;
    const model = tableModel(this.text), old = t.model;
    Object.assign(t, { widget: this, view });
    // Compared with what's drawn: a row or column change edits the model before the text comes back.
    const drawnRows = t.grid.querySelectorAll("tr");
    const same = model.rows.length === drawnRows.length && model.aligns.length === (drawnRows[0]?.cells.length ?? 0);
    const active = dom.contains(document.activeElement) && document.activeElement.closest("[data-r]");
    if (same && active) model.rows[+active.dataset.r][+active.dataset.c] = old.rows[+active.dataset.r][+active.dataset.c];
    t.model = model;
    if (!same) { fillTable(dom); return true; }
    for (const cell of t.grid.querySelectorAll("[data-r]")) {
      const raw = model.rows[+cell.dataset.r][+cell.dataset.c], align = model.aligns[+cell.dataset.c];
      if (align) cell.setAttribute("align", align); else cell.removeAttribute("align");
      if (cell === active) continue;
      if (cell.__raw !== raw) { cell.__raw = raw; cell.innerHTML = cellHtml(raw, this.resolve); }
    }
    return true;
  }
  ignoreEvent() { return true; }
}

function fillTable(block) {
  const { model, grid, widget } = block.__table;
  const table = document.createElement("table"), head = table.createTHead(), body = table.createTBody();
  model.rows.forEach((row, r) => {
    const tr = (r ? body : head).insertRow();
    row.forEach((raw, c) => {
      const cell = document.createElement(r ? "td" : "th");
      Object.assign(cell.dataset, { r, c });
      cell.contentEditable = "plaintext-only";
      cell.spellcheck = true;
      if (model.aligns[c]) cell.setAttribute("align", model.aligns[c]);
      cell.__raw = raw;
      cell.innerHTML = cellHtml(raw, widget.resolve);
      tr.appendChild(cell);
    });
  });
  const add = (kind, label) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = `md-table-add is-${kind}`; b.dataset.add = kind; b.tabIndex = -1;
    b.setAttribute("aria-label", label); b.textContent = "+";
    return b;
  };
  grid.replaceChildren(table, add("column", "Add a column"), add("row", "Add a row"));
}

function openCell(block, cell) {
  const raw = block.__table.model.rows[+cell.dataset.r][+cell.dataset.c];
  if (cell.textContent !== raw) cell.textContent = raw;  // plain text renders as itself: the caret stays put
}
function closeCell(block, cell) {
  const raw = block.__table.model.rows[+cell.dataset.r][+cell.dataset.c];
  cell.__raw = raw;
  cell.innerHTML = cellHtml(raw, block.__table.widget.resolve);
}

// Where the table is in the document now: the widget's position and the text it was drawn from.
function tableRange(block) {
  const { view, widget } = block.__table, from = view.posAtDOM(block);
  return view.state.sliceDoc(from, from + widget.text.length) === widget.text ? { from, to: from + widget.text.length } : null;
}
function commitTable(block) {
  const { view, model, widget } = block.__table, range = tableRange(block), text = tableText(model);
  if (!range || text === widget.text) return;
  view.dispatch({ changes: { from: range.from, to: range.to, insert: text }, userEvent: "input.table" });
}
function reshapeTable(block, change, [r, c]) {
  change(block.__table.model);
  commitTable(block);
  focusCell(block, r, c);
}
function focusCell(block, r, c, atEnd = true) {
  const cell = block.__table.grid.querySelector(`[data-r="${r}"][data-c="${c}"]`);
  if (!cell) return;
  cell.focus({ preventScroll: false });
  const sel = getSelection(), range = document.createRange();
  range.selectNodeContents(cell);
  range.collapse(!atEnd);
  sel.removeAllRanges(); sel.addRange(range);
}
// Out of the table to the line above or below it (adding one at the document's edge).
function leaveTable(block, dir) {
  const { view } = block.__table, range = tableRange(block);
  if (!range) return;
  view.focus();
  if (dir < 0) {
    if (range.from === 0) view.dispatch({ changes: { from: 0, insert: "\n" }, selection: { anchor: 0 } });
    else view.dispatch({ selection: { anchor: range.from - 1 }, scrollIntoView: true });
  } else if (range.to >= view.state.doc.length) view.dispatch({ changes: { from: range.to, insert: "\n" }, selection: { anchor: range.to + 1 } });
  else view.dispatch({ selection: { anchor: range.to + 1 }, scrollIntoView: true });
}
// The grid of the table that starts at `pos`, if one is drawn.
function tableAtPos(view, pos) {
  for (const el of view.contentDOM.querySelectorAll(".md-block-table")) if (el.__table && view.posAtDOM(el) === pos) return el;
  return null;
}

function caretAt(cell) {
  const sel = getSelection();
  if (!sel.rangeCount || !sel.isCollapsed) return null;
  const range = document.createRange();
  range.selectNodeContents(cell);
  range.setEnd(sel.getRangeAt(0).startContainer, sel.getRangeAt(0).startOffset);
  return range.toString().length;
}
function tableKey(block, e) {
  const cell = e.target.closest("[data-r]");
  if (!cell || e.isComposing) return;
  const { model } = block.__table, r = +cell.dataset.r, c = +cell.dataset.c;
  const rows = model.rows.length, cols = model.aligns.length, at = caretAt(cell), len = cell.textContent.length;
  const go = (row, col, atEnd) => { e.preventDefault(); focusCell(block, row, col, atEnd); };
  const newRow = (col) => { e.preventDefault(); reshapeTable(block, (m) => m.rows.push(m.aligns.map(() => "")), [rows, col]); };
  if (e.key === "Tab" && !e.shiftKey) return c + 1 < cols ? go(r, c + 1) : r + 1 < rows ? go(r + 1, 0) : newRow(0);
  if (e.key === "Tab") { e.preventDefault(); return c > 0 ? go(r, c - 1) : r > 0 ? go(r - 1, cols - 1) : undefined; }
  if (e.key === "Enter" && !e.metaKey) return r + 1 < rows ? go(r + 1, c) : newRow(c);
  if (e.key === "Escape") { e.preventDefault(); return leaveTable(block, 1); }
  if (e.key === "ArrowUp" && !e.shiftKey) { e.preventDefault(); return r > 0 ? go(r - 1, c) : leaveTable(block, -1); }
  if (e.key === "ArrowDown" && !e.shiftKey) { e.preventDefault(); return r + 1 < rows ? go(r + 1, c) : leaveTable(block, 1); }
  if (e.key === "ArrowLeft" && !e.shiftKey && at === 0) {
    e.preventDefault();
    return c > 0 ? go(r, c - 1) : r > 0 ? go(r - 1, cols - 1) : leaveTable(block, -1);
  }
  if (e.key === "ArrowRight" && !e.shiftKey && at === len) {
    e.preventDefault();
    return c + 1 < cols ? go(r, c + 1, false) : r + 1 < rows ? go(r + 1, 0, false) : leaveTable(block, 1);
  }
  if (e.key === "Backspace" && at === 0 && r > 0 && model.rows[r].every((x) => !x.trim())) {
    e.preventDefault();
    return reshapeTable(block, (m) => m.rows.splice(r, 1), [r - 1, c]);
  }
}

function tableMenu(block, e) {
  const cell = e.target.closest("[data-r]"), { view, model } = block.__table, open = view.dom.__lunaMenu;
  if (!cell || !open) return;
  const r = +cell.dataset.r, c = +cell.dataset.c, blank = () => model.aligns.map(() => "");
  const align = (a) => ({ label: { left: "Align left", center: "Align centre", right: "Align right" }[a], meta: model.aligns[c] === a ? "✓" : "",
    onClick: () => reshapeTable(block, (m) => { m.aligns[c] = m.aligns[c] === a ? "" : a; }, [r, c]) });
  open(e, [
    { label: "Insert row above", onClick: () => reshapeTable(block, (m) => m.rows.splice(Math.max(r, 1), 0, blank()), [Math.max(r, 1), c]) },
    { label: "Insert row below", onClick: () => reshapeTable(block, (m) => m.rows.splice(r + 1, 0, blank()), [r + 1, c]) },
    model.rows.length > 2 && { label: "Delete row", onClick: () => reshapeTable(block, (m) => m.rows.splice(r, 1), [Math.min(r, model.rows.length - 2), c]) },
    "-",
    { label: "Insert column left", onClick: () => reshapeTable(block, (m) => { m.aligns.splice(c, 0, ""); m.rows.forEach((x) => x.splice(c, 0, "")); }, [r, c]) },
    { label: "Insert column right", onClick: () => reshapeTable(block, (m) => { m.aligns.splice(c + 1, 0, ""); m.rows.forEach((x) => x.splice(c + 1, 0, "")); }, [r, c + 1]) },
    model.aligns.length > 1 && { label: "Delete column", onClick: () => reshapeTable(block, (m) => { m.aligns.splice(c, 1); m.rows.forEach((x) => x.splice(c, 1)); }, [r, Math.min(c, model.aligns.length - 2)]) },
    "-", align("left"), align("center"), align("right"),
    "-",
    { label: "Delete table", danger: true, onClick: () => {
      const range = tableRange(block);
      if (!range) return;
      view.focus();
      view.dispatch({ changes: { from: range.from, to: Math.min(range.to + 1, view.state.doc.length) }, selection: { anchor: range.from } });
    } },
  ]);
}

// The mode is state, so decorations follow it alone: nothing depends on where the cursor is. Focus is state too, for
// the inline maths tip.
const setMode = StateEffect.define();
const mode = StateField.define({
  create: () => "read",
  update: (v, tr) => { for (const e of tr.effects) if (e.is(setMode)) v = e.value; return v; },
});
const editing = (state) => state.field(mode) === "edit";
const setFocus = StateEffect.define();
const focused = StateField.define({
  create: () => false,
  update: (v, tr) => { for (const e of tr.effects) if (e.is(setFocus)) v = e.value; return v; },
});
const modeChanged = (tr) => tr.effects.some((e) => e.is(setMode));

// Blocks span lines, so they live in a state field: code fences (``` lang … ```), display maths ($$ … $$), columns
// (::: columns … +++ … :::), callouts (::: kind Title … :::) and tables. Nothing inside a code fence starts another block.
const FENCE = /^\s*(`{3,}|~{3,})\s*([^`]*)$/;
const CONTAINER = /^:::\s*[a-z]+(\s|$)/i;
// A GFM table: a header row, then a delimiter row with as many cells (| --- | :---: |), then rows up to a blank line or
// a line without a pipe.
const DELIMITER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
function cellsOf(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map((c) => c.trim());
}
const tableAt = (header, delimiter) => header.includes("|") && delimiter.includes("|") && DELIMITER.test(delimiter)
  && cellsOf(header).length === cellsOf(delimiter).length;
const tableRow = (text) => text.includes("|") && !!text.trim() && !FENCE.test(text) && !CONTAINER.test(text.trim());

// The line that closes the block opened on line n, or 0. Without limits a stray opener pairs with the next block's own
// delimiter far below and swallows everything between, so: containers nest (an opener inside needs its own :::), and
// display maths stops at a blank line or another block, where it can't continue anyway.
function closing(doc, n, { close, stop, nest }) {
  let fence = null, depth = 0;
  for (let m = n + 1; m <= doc.lines; m++) {
    const text = doc.line(m).text, open = FENCE.exec(text), t = text.trim();
    if (fence) { if (open && open[1][0] === fence[0] && open[1].length >= fence.length && !open[2].trim()) fence = null; continue; }
    if (stop && stop(t)) return 0;
    if (open) { fence = open[1]; continue; }
    if (nest && nest.test(t)) depth++;
    else if (close(t)) { if (!depth) return m; depth--; }
  }
  return 0;
}
const MATH_END = { close: (t) => t === "$$", stop: (t) => !t || FENCE.test(t) || CONTAINER.test(t) || t === ":::" };
const CONTAINER_END = { close: (t) => t === ":::", nest: CONTAINER };

function blocks(state) {
  const out = [], doc = state.doc;
  const lines = (a, b) => { const body = []; for (let k = a; k <= b; k++) body.push(doc.line(k).text); return body.join("\n"); };
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n), text = line.text.trim(), fence = FENCE.exec(line.text);
    if (fence) {
      let end = 0;
      for (let m = n + 1; m <= doc.lines && !end; m++) {
        const close = FENCE.exec(doc.line(m).text);
        if (close && close[1][0] === fence[1][0] && close[1].length >= fence[1].length && !close[2].trim()) end = m;
      }
      if (!end) break;  // an unclosed fence runs to the end: leave it all as source
      const info = fence[2].trim(), kind = info.split(/\s/)[0].toLowerCase() === "component" ? "component" : "code";
      out.push({ kind, info, code: lines(n + 1, end - 1), from: line.from, to: doc.line(end).to, lines: [n, end] });
      n = end;
    } else if (text.startsWith("$$")) {
      if (text.length > 4 && text.endsWith("$$")) { out.push({ kind: "math", from: line.from, to: line.to, source: text.slice(2, -2), lines: [n, n] }); continue; }
      if (text !== "$$") continue;
      const end = closing(doc, n, MATH_END);
      if (!end) continue;
      out.push({ kind: "math", from: line.from, to: doc.line(end).to, source: lines(n + 1, end - 1), lines: [n, end] });
      n = end;
    } else if (/^:::\s*columns\s*$/.test(text)) {
      const end = closing(doc, n, CONTAINER_END);
      if (!end) continue;
      const columns = [[]];
      for (let k = n + 1; k < end; k++) {
        const row = doc.line(k).text;
        if (row.trim() === "+++") columns.push([]); else columns[columns.length - 1].push(row);
      }
      out.push({ kind: "columns", from: line.from, to: doc.line(end).to, columns, lines: [n, end] });
    } else if (CONTAINER.test(text)) {
      const [, type, title] = /^:::\s*([a-z]+)\s*(.*)$/i.exec(text);
      const end = closing(doc, n, CONTAINER_END);
      if (!end) continue;
      out.push({ kind: "callout", type: type.toLowerCase(), title, from: line.from, to: doc.line(end).to, body: lines(n + 1, end - 1), lines: [n, end] });
    } else if (n < doc.lines && tableAt(line.text, doc.line(n + 1).text)) {
      let end = n + 1;
      while (end < doc.lines && tableRow(doc.line(end + 1).text)) end++;
      out.push({ kind: "table", from: line.from, to: doc.line(end).to, text: lines(n, end), lines: [n, end] });
      n = end;
    }
  }
  // Containers' lines are scanned too, so Edit mode can frame and preview what's inside them; `within` names the
  // container a block sits in (Read mode renders it as part of that container).
  for (const b of out) b.within = out.find((c) => c !== b && (c.kind === "columns" || c.kind === "callout") && b.from > c.from && b.to <= c.to);
  return out;
}

// A block the cursor is in shows its source, framed: code, callouts, tables and display maths. `skip` holds the ranges
// inside it that render (a callout's paragraphs or code the cursor isn't in).
function frame(state, b, set, skip = []) {
  const [first, last] = b.lines;
  const kind = b.kind === "callout" ? calloutHead(b.type, b.title).kind : null;
  const base = b.kind === "code" || b.kind === "component" ? "md-codeblock" : b.kind === "math" ? "md-mathsrc" : b.kind === "table" ? "md-tablesrc"
    : `md-box-src md-box-${kind}`;
  for (let n = first; n <= last; n++) {
    const at = state.doc.line(n).from;
    if (skip.some((r) => at >= r.from && at <= r.to)) continue;
    const cls = [base, n === first && "is-first", n === last && "is-last"];
    set.push(Decoration.line({ class: cls.filter(Boolean).join(" ") }).range(at));
  }
  if (b.kind === "callout") {
    const head = state.doc.line(first), m = /^(\s*:::\s*)([a-z]+)/i.exec(head.text);
    if (m) set.push(Decoration.mark({ class: "md-box-type" }).range(head.from + m[1].length, head.from + m[1].length + m[2].length));
  }
}

// The lines the cursor (or a selection) is on, as [first, last] line numbers: in Edit mode they show their source and
// everything else renders, as it does in Read. Blocks count as one area: any of their lines in it shows all of them.
const liveLines = (state) => (editing(state)
  ? state.selection.ranges.map((r) => [state.doc.lineAt(r.from).number, state.doc.lineAt(r.to).number]) : []);
const isLive = (lines, first, last = first) => lines.some(([a, z]) => a <= last && z >= first);

// A line being edited that names an image shows the image under it (outside code, which the field leaves as source).
const IMAGE = /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g;
function imagesBelow(state, resolve, set, lines, skip) {
  for (const [a, z] of lines) for (let n = a; n <= z; n++) {
    const line = state.doc.line(n);
    if (!line.text.includes("![") || skip.some((r) => line.from >= r.from && line.from <= r.to) || inCode(state, line.from)) continue;
    for (const m of line.text.matchAll(IMAGE)) {
      set.push(Decoration.widget({ widget: new ImageWidget(resolve(m[2]), m[1], true), block: true, side: 1 }).range(line.to));
    }
  }
}

// A block rendered inside a callout whose source is showing sits in that callout's box, so the frame stays whole.
class InBoxWidget extends WidgetType {
  constructor(widget, kind) { super(); this.widget = widget; this.kind = kind; }
  eq(o) { return o.kind === this.kind && o.widget.constructor === this.widget.constructor && this.widget.eq(o.widget); }
  get estimatedHeight() { return this.widget.estimatedHeight; }
  toDOM(view) {
    const box = document.createElement("div");
    box.className = `md-box-src md-box-${this.kind} md-box-inner`;
    box.appendChild(this.widget.toDOM(view));
    return box;
  }
  ignoreEvent() { return false; }
}

// Blocks render unless the cursor is in them (Read: always). Containers nest like capsules: inside a callout its frame
// and source show, while its other paragraphs and blocks render on their own; leave it and the whole callout renders.
function blockField(resolve) {
  const decorate = (state, ranges) => {
    const set = [], lines = liveLines(state), live = (b) => isLive(lines, b.lines[0], b.lines[1]);
    // Inside a container that renders, a block is drawn by that container.
    const drawnByContainer = (b) => { for (let c = b.within; c; c = c.within) if (!live(c)) return true; return false; };
    const rendered = [];
    for (const b of ranges) {
      const grid = b.kind === "table" && editing(state);
      if (drawnByContainer(b) || (live(b) && !grid)) continue;
      let widget = grid ? new TableEditWidget(b.text, resolve)
        : b.kind === "math" ? new MathWidget(b.source, true)
        : b.kind === "code" ? new CodeWidget(b.info, b.code)
        : b.kind === "component" ? new ComponentWidget(b.code)
        : b.kind === "callout" ? new CalloutWidget(b.type, b.title, b.body, resolve)
        : b.kind === "table" ? new TableWidget(b.text, resolve)
        : new ColumnsWidget(b.columns, resolve);
      if (b.within && b.within.kind === "callout") widget = new InBoxWidget(widget, calloutHead(b.within.type, b.within.title).kind);
      set.push(Decoration.replace({ widget, block: true }).range(b.from, b.to));
      rendered.push(b);
    }
    for (const b of ranges) {
      if (drawnByContainer(b) || !live(b) || b.kind === "table") continue;
      // A callout's frame is the frame for what's inside it; columns have none, so their contents get their own.
      if (b.kind !== "columns" && !(b.within && b.within.kind === "callout")) frame(state, b, set, rendered);
      if (b.kind === "math" && b.source.trim()) set.push(Decoration.widget({ widget: new MathWidget(b.source, true, true), block: true, side: 1 }).range(b.to));
      if (b.kind === "component" && !b.within && b.code.trim()) set.push(Decoration.widget({ widget: new ComponentWidget(b.code, true), block: true, side: 1 }).range(b.to));
    }
    if (lines.length) imagesBelow(state, resolve, set, lines, rendered);
    return { decorations: Decoration.set(set, true), ranges, rendered, key: JSON.stringify(lines) };
  };
  return StateField.define({
    create: (state) => decorate(state, blocks(state)),
    update(value, tr) {
      if (tr.docChanged) return decorate(tr.state, blocks(tr.state));
      if (modeChanged(tr) || (tr.selection && JSON.stringify(liveLines(tr.state)) !== value.key)) return decorate(tr.state, value.ranges);
      return value;
    },
    provide: (f) => EditorView.decorations.from(f, (v) => v.decorations),
  });
}

// ↑/↓ onto a rendered block step into it (its first line going down, its last going up), which shows its source; left
// alone, CodeMirror would jump over it as if it were one line.
function enterBlock(field, dir) {
  return (view) => {
    const { state } = view, r = state.selection.main;
    if (!editing(state) || !r.empty) return false;
    const line = state.doc.lineAt(r.head), n = line.number + dir;
    if (n < 1 || n > state.doc.lines) return false;
    const next = state.doc.line(n), b = state.field(field).rendered.find((x) => next.from >= x.from && next.from <= x.to);
    if (!b) return false;
    const grid = b.kind === "table" && tableAtPos(view, b.from);
    if (grid) { focusCell(grid, dir > 0 ? 0 : grid.__table.model.rows.length - 1, 0); return true; }
    const target = state.doc.line(dir > 0 ? b.lines[0] : b.lines[1]);
    view.dispatch({ selection: { anchor: target.from }, scrollIntoView: true, userEvent: "select" });
    return true;
  };
}

// ←/→ onto rendered inline maths step just inside its `$`, which opens its source; CodeMirror would skip over it whole.
function enterMath(dir) {
  return (view) => {
    const { state } = view, r = state.selection.main;
    if (!editing(state) || !r.empty) return false;
    for (let node = syntaxTree(state).resolveInner(r.head, dir); node; node = node.parent) {
      if (node.name !== "InlineMath") continue;
      if (r.head !== (dir < 0 ? node.to : node.from)) return false;
      const n = mathMarks(state.doc, node);
      view.dispatch({ selection: { anchor: dir < 0 ? node.to - n : node.from + n }, scrollIntoView: true, userEvent: "select" });
      return true;
    }
    return false;
  };
}

// Inline: both modes style headings and quotes by line and draw task boxes. Read, and in Edit every line the cursor isn't
// on, also hides the marks and swaps in maths, images, bullets and rules.
const HIDE = new Set(["HeaderMark", "EmphasisMark", "CodeMark", "StrikethroughMark", "QuoteMark", "UnderlineMark"]);
// On the line being edited, bold, italics, underline and strikethrough still keep their marks only while the cursor is
// inside the span, so finished text reads formatted as you go.
const SPAN_MARKS = new Set(["EmphasisMark", "StrikethroughMark", "UnderlineMark"]);
const hidden = Decoration.replace({});
const CODE_NODES = new Set(["FencedCode", "CodeBlock", "InlineCode", "InlineMath"]);
function inCode(state, pos) {
  for (let n = syntaxTree(state).resolveInner(pos, 1); n; n = n.parent) if (CODE_NODES.has(n.name)) return true;
  return false;
}

function inline(view, field, resolve) {
  const { state } = view, set = [], edit = editing(state), lines = liveLines(state);
  const covered = state.field(field).rendered;
  // Whether pos is on a line being edited, which shows its source.
  const src = (pos) => edit && isLive(lines, state.doc.lineAt(pos).number);
  // Only nodes wholly inside a rendered block are skipped (the document itself starts inside a leading one).
  const inBlock = (from, to) => covered.some((b) => from >= b.from && to <= b.to);
  const touched = (a, b) => state.selection.ranges.some((r) => (r.empty ? r.head > a && r.head < b : r.from < b && r.to > a));
  const lineClass = (pos, cls) => set.push(Decoration.line({ class: cls }).range(state.doc.lineAt(pos).from));
  // A blank line between paragraphs is a paragraph gap, not a line of text (code keeps its blank lines). In Read mode more
  // blank lines in a row add nothing, as Markdown reads any run as one break; in Edit each keeps its gap, so the cursor
  // always has a line to sit on.
  const inFence = (pos) => { for (let n = syntaxTree(state).resolveInner(pos, 1); n; n = n.parent) if (n.name === "FencedCode") return true; return false; };
  const blank = (n) => n >= 1 && !state.doc.line(n).text.trim();
  for (const { from, to } of state.doc.lines < 2 ? [] : view.visibleRanges) {
    for (let pos = from; pos <= to;) {
      const line = state.doc.lineAt(pos);
      if (blank(line.number) && !inBlock(line.from, line.to) && !inFence(line.from)) lineClass(line.from, !edit && blank(line.number - 1) ? "md-gap md-gap-more" : "md-gap");
      pos = line.to + 1;
    }
  }
  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({ from, to, enter(node) {
      if (inBlock(node.from, node.to)) return false;
      const name = node.name;
      if (/^ATXHeading(\d)$/.test(name)) lineClass(node.from, `md-h md-h${name.slice(-1)}`);
      else if (name === "Blockquote") {
        // Obsidian-style callouts: "> [!note] Title" lights the whole quote; the [!note] tag hides away from the cursor.
        const first = state.doc.lineAt(node.from), tag = /^(\s*>\s*)(\[!(\w+)\]\s*)(.*)$/.exec(first.text);
        for (let p = node.from; p <= node.to;) { const l = state.doc.lineAt(p); lineClass(l.from, tag ? "md-quote md-callout" : "md-quote"); p = l.to + 1; }
        if (tag) {
          const at = first.from + tag[1].length;
          if (!src(at)) set.push(hidden.range(at, at + tag[2].length));
          if (tag[4]) set.push(Decoration.mark({ class: "md-callout-title" }).range(at + tag[2].length, first.to));
        }
      } else if (name === "FencedCode") {
        return false;  // framed by the block field; its own language highlights it
      } else if (SPAN_MARKS.has(name) && src(node.from)) {
        const span = node.node.parent;
        if (span && !touched(span.from, span.to)) set.push(hidden.range(node.from, node.to));
      } else if (name === "HorizontalRule" && !src(node.from)) set.push(Decoration.replace({ widget: new RuleWidget() }).range(node.from, node.to));
      else if (HIDE.has(name) && !src(node.from)) {
        // A heading's mark takes its trailing space with it.
        const end = name === "HeaderMark" && state.doc.sliceString(node.to, node.to + 1) === " " ? node.to + 1 : node.to;
        set.push(hidden.range(node.from, end));
      } else if (name === "ListMark" && !src(node.from)) {
        const item = node.node.parent, task = item && item.getChild("Task");
        const mark = state.doc.sliceString(node.from, node.to);
        if (task) set.push(hidden.range(node.from, Math.min(node.to + 1, task.from)));
        else if (mark === "-" || mark === "*" || mark === "+") set.push(Decoration.replace({ widget: new BulletWidget() }).range(node.from, node.to));
      } else if (name === "TaskMarker") {
        const done = /x/i.test(state.doc.sliceString(node.from, node.to));
        set.push(Decoration.replace({ widget: new CheckWidget(done, node.from) }).range(node.from, node.to));
        const text = state.doc.sliceString(node.to, state.doc.lineAt(node.to).to), lead = text.length - text.trimStart().length;
        if (done && text.trim()) set.push(Decoration.mark({ class: "md-done" }).range(node.to + lead, node.to + text.length));
      } else if (name === "InlineMath") {
        // Inline maths renders the moment the cursor leaves it (typing its closing `$` included), not when it leaves the line.
        if (!(edit && touched(node.from, node.to))) {
          const n = mathMarks(state.doc, node);
          set.push(Decoration.replace({ widget: new MathWidget(state.doc.sliceString(node.from + n, node.to - n), false, null, false, n === 2) }).range(node.from, node.to));
        }
        return false;
      } else if (name === "Image") {
        if (!src(node.from)) {
          const text = state.doc.sliceString(node.from, node.to), m = /^!\[([^\]]*)\]\(([^)\s]+)/.exec(text);
          if (m) set.push(Decoration.replace({ widget: new ImageWidget(resolve(m[2]), m[1]) }).range(node.from, node.to));
        }
        return false;
      } else if (name === "Link") {
        if (state.doc.sliceString(node.from, node.from + 2) === "[!") return false;  // a callout tag, not a link
        if (!src(node.from)) {
          const marks = node.node.getChildren("LinkMark");
          if (marks.length >= 2) {
            set.push(hidden.range(marks[0].from, marks[0].to));
            set.push(Decoration.mark({ class: "md-link" }).range(marks[0].to, marks[1].from));
            set.push(hidden.range(marks[1].from, node.to));
          }
        }
        return false;
      }
    } });
  }
  return Decoration.set(set, true);
}

const highlight = HighlightStyle.define([
  { tag: t.strong, class: "md-strong" }, { tag: t.emphasis, class: "md-em" }, { tag: t.strikethrough, class: "md-strike" },
  { tag: underlineTag, class: "md-underline" },
  { tag: t.monospace, class: "md-code" }, { tag: t.link, class: "md-link" }, { tag: t.url, class: "md-url" },
  { tag: t.processingInstruction, class: "md-mark" }, { tag: t.meta, class: "md-mark" },
]);

// In Edit mode, inline maths under the cursor shows its rendering in a tip just above (over the page, so nothing moves).
function inlineMathAt(state) {
  if (!editing(state) || !state.field(focused) || !state.selection.main.empty) return null;
  const head = state.selection.main.head, tree = syntaxTree(state);
  for (const side of [-1, 1]) {
    for (let node = tree.resolveInner(head, side); node; node = node.parent) {
      if (node.name === "InlineMath") {
        if (head <= node.from || head >= node.to) return null;
        const n = mathMarks(state.doc, node);
        return { from: node.from, source: state.doc.sliceString(node.from + n, node.to - n), wide: n === 2 };
      }
    }
  }
  return null;
}
const mathTip = StateField.define({
  create: (state) => inlineMathAt(state),
  update: (value, tr) => (tr.docChanged || tr.selection || modeChanged(tr) || tr.effects.some((e) => e.is(setFocus)) ? inlineMathAt(tr.state) : value),
  provide: (f) => showTooltip.compute([f], (state) => {
    const m = state.field(f);
    return m && { pos: m.from, above: true, arrow: false, create: () => {
      const dom = document.createElement("div");
      dom.className = "md-math-tip";
      dom.innerHTML = tex(m.source, m.wide);
      return { dom };
    } };
  }),
});

// Typing the third backtick of an opening fence adds the closing one below, so the rest of the note never turns into
// code while you name the language. Not when it would close an open fence, or pair with an unmatched one below.
function openFences(doc, from, to) {
  let fence = null;
  for (let m = from; m <= to; m++) {
    const o = FENCE.exec(doc.line(m).text);
    if (!o) continue;
    if (fence) { if (o[1][0] === fence[0] && o[1].length >= fence.length && !o[2].trim()) fence = null; } else fence = o[1];
  }
  return !!fence;
}
const closeFence = EditorView.inputHandler.of((view, from, to, text) => {
  if (text !== "`" || from !== to) return false;
  const { doc } = view.state, line = doc.lineAt(from);
  const typed = line.text.slice(0, from - line.from) + "`" + line.text.slice(from - line.from);
  if (typed.trim() !== "```" || openFences(doc, 1, line.number - 1) || openFences(doc, line.number + 1, doc.lines)) return false;
  const indent = /^\s*/.exec(line.text)[0];
  view.dispatch({ changes: [{ from, insert: "`" }, { from: line.to, insert: `\n${indent}\`\`\`` }], selection: { anchor: from + 1 }, userEvent: "input.type" });
  return true;
});

// Formatting ----------------------------------------------------------------------------------------------------------

// ⌘B, ⌘I, ⌘U (underline), ⌘⇧C (code), ⌘⇧S (strikethrough), ⌘⇧E (maths), Notion's keys except code (⌘E switches modes):
// wrap the selection in the marks, or unwrap it when it's already wrapped (marks just outside or at the edges of the
// selection). With nothing selected the marks go in around the cursor; pressed again right away they go, at the end of
// the span the cursor steps out past its closing marks (as in Obsidian), and anywhere else inside it the span is
// unwrapped. Stars are counted, so ⌘I inside **bold** adds italics rather than eating a star.
const present = (run, mark) => (mark === "**" ? run >= 2 : mark === "*" ? run % 2 === 1 : run >= mark.length);
function runOf(doc, pos, ch, dir) {
  let n = 0;
  while (n < 3 && doc.sliceString(dir < 0 ? pos - n - 1 : pos + n, dir < 0 ? pos - n : pos + n + 1) === ch) n++;
  return n;
}
const SPANS = { "__": ["Underline", "UnderlineMark"], "**": ["StrongEmphasis", "EmphasisMark"], "*": ["Emphasis", "EmphasisMark"], "~~": ["Strikethrough", "StrikethroughMark"],
  "`": ["InlineCode", "CodeMark"], "$": ["InlineMath", "InlineMathMark"] };
// The span of this kind the cursor is inside (between its marks): its opening and closing marks' ranges.
function spanAt(state, pos, mark) {
  const [name, markName] = SPANS[mark];
  for (const side of [-1, 1]) {
    for (let node = syntaxTree(state).resolveInner(pos, side); node; node = node.parent) {
      if (node.name !== name) continue;
      const marks = node.getChildren(markName);
      if (marks.length < 2) break;
      const open = marks[0], close = marks[marks.length - 1];
      if (pos >= open.to && pos <= close.from) return { open: [open.from, open.to], close: [close.from, close.to] };
      break;
    }
  }
  return null;
}
function toggle(mark) {
  const n = mark.length, ch = mark[0];
  return (view) => {
    const { state } = view, doc = state.doc;
    view.dispatch(state.changeByRange(({ from, to, empty }) => {
      // Marks right outside from…to, or at its inner edges.
      if (present(Math.min(runOf(doc, from, ch, -1), runOf(doc, to, ch, 1)), mark)) {
        return { changes: [{ from: from - n, to: from }, { from: to, to: to + n }], range: EditorSelection.range(from - n, to - n) };
      }
      if (empty) {
        const span = spanAt(state, from, mark);
        if (span && from === span.close[0]) return { range: EditorSelection.cursor(span.close[1]) };
        if (span) {
          return { changes: [{ from: span.open[0], to: span.open[1] }, { from: span.close[0], to: span.close[1] }],
            range: EditorSelection.cursor(from - (span.open[1] - span.open[0])) };
        }
      } else if (to - from > 2 * n && present(Math.min(runOf(doc, from, ch, 1), runOf(doc, to, ch, -1)), mark)) {
        return { changes: [{ from, to: from + n }, { from: to - n, to }], range: EditorSelection.range(from, to - 2 * n) };
      }
      return { changes: [{ from, insert: mark }, { from: to, insert: mark }], range: EditorSelection.range(from + n, to + n) };
    }), { userEvent: "input.format", scrollIntoView: true });
    return true;
  };
}
// ⌘K: the selection becomes the link's text with the cursor where the address goes, or, when the selection is an
// address, its target with the cursor in the text.
function link(view) {
  const { state } = view, r = state.selection.main, text = state.sliceDoc(r.from, r.to);
  const url = /^(https?:\/\/|mailto:)\S+$/.test(text);
  const insert = url ? `[](${text})` : `[${text}]()`;
  view.dispatch({ changes: { from: r.from, to: r.to, insert }, selection: { anchor: url || !text ? r.from + 1 : r.from + text.length + 3 },
    userEvent: "input.format" });
  return true;
}
const FORMAT_KEYS = [
  { key: "Mod-b", run: toggle("**") }, { key: "Mod-i", run: toggle("*") }, { key: "Mod-u", run: toggle("__") },
  { key: "Mod-Shift-c", run: toggle("`") },
  { key: "Mod-Shift-s", run: toggle("~~") }, { key: "Mod-Shift-e", run: toggle("$") }, { key: "Mod-k", run: link },
];
// Typing a mark over a selection wraps it instead of replacing it: * _ ` ~ $ = (brackets wrap through closeBrackets).
const wrapSelection = EditorView.inputHandler.of((view, from, to, text) => {
  const { state } = view;
  if (!"*_`~$=".includes(text) || text.length !== 1 || state.selection.ranges.some((r) => r.empty)) return false;
  view.dispatch(state.changeByRange((r) => ({ changes: [{ from: r.from, insert: text }, { from: r.to, insert: text }],
    range: EditorSelection.range(r.from + 1, r.to + 1) })), { userEvent: "input.type" });
  return true;
});
// Brackets pair as you type; quotes don't (they'd trip over every apostrophe).
const pairs = EditorState.languageData.of(() => [{ closeBrackets: { brackets: ["(", "[", "{"] } }]);

// LaTeX commands, offered after a backslash inside maths. Templates' ${} are the fields Tab steps through; the menu shows
// each one rendered.
const SYMBOLS = ("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi rho sigma tau "
  + "upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega infty partial nabla cdot times div "
  + "pm mp leq geq neq approx equiv sim propto in notin subset subseteq cup cap setminus emptyset forall exists neg land "
  + "lor implies iff to mapsto rightarrow leftarrow Rightarrow Leftrightarrow langle rangle lVert rVert ldots cdots vdots "
  + "ddots top perp mid quad qquad log ln exp sin cos tan max min arg det sup inf lim sum prod int oint").split(" ");
const TEMPLATES = ["\\frac{${}}{${}}", "\\dfrac{${}}{${}}", "\\sqrt{${}}", "\\binom{${}}{${}}", "\\mathbb{${}}", "\\mathcal{${}}",
  "\\mathbf{${}}", "\\mathrm{${}}", "\\text{${}}", "\\operatorname{${}}", "\\hat{${}}", "\\bar{${}}", "\\vec{${}}",
  "\\tilde{${}}", "\\dot{${}}", "\\overline{${}}", "\\underbrace{${}}_{${}}", "\\left( ${} \\right)", "\\left[ ${} \\right]",
  "\\left\\{ ${} \\right\\}", "\\begin{aligned}\n${}\n\\end{aligned}", "\\begin{pmatrix}\n${}\n\\end{pmatrix}",
  "\\begin{bmatrix}\n${}\n\\end{bmatrix}", "\\begin{cases}\n${}\n\\end{cases}"];
// Each template ends in one more field, so the last Tab steps out past the closing brace.
const TEX_OPTIONS = [
  ...TEMPLATES.map((tpl) => snippetCompletion(`${tpl}\${}`, { label: /^\\(begin\{\w+\}|left\\?.|[a-zA-Z]+)/.exec(tpl)[0], type: "tex",
    preview: tpl.replace(/\$\{\}/g, "x").replace(/\n/g, " ") })),
  ...SYMBOLS.map((name) => ({ label: `\\${name}`, type: "tex", preview: `\\${name}` })),
];
function latex(field) {
  const inMath = (state, pos) => {
    if (state.field(field).ranges.some((b) => b.kind === "math" && pos > b.from && pos <= b.to)) return true;
    const line = state.doc.lineAt(pos), before = line.text.slice(0, pos - line.from).replace(/\\\$/g, "").replace(/\$\$/g, "$");
    return (before.match(/\$/g) || []).length % 2 === 1;
  };
  return (context) => {
    const word = context.matchBefore(/\\[a-zA-Z]*$/);
    if (!word || context.state.sliceDoc(word.from - 1, word.from) === "\\" || !inMath(context.state, context.pos)) return null;
    return { from: word.from, options: TEX_OPTIONS, validFor: /^\\[a-zA-Z]*$/ };
  };
}
const glyphs = new Map();
function glyph(c) {
  const g = document.createElement("span");
  g.className = "md-slash-glyph";
  if (c.preview) {
    if (!glyphs.has(c.preview)) glyphs.set(c.preview, tex(c.preview, false));
    g.innerHTML = glyphs.get(c.preview);
  } else g.textContent = c.glyph;
  return g;
}

// The / menu ----------------------------------------------------------------------------------------------------------

const BLOCKS = [
  ["Heading 1", "Big section heading", "H1", "# "], ["Heading 2", "Medium section heading", "H2", "## "],
  ["Heading 3", "Small section heading", "H3", "### "], ["To-do list", "Track tasks with checkboxes", "☐", "- [ ] "],
  ["Bulleted list", "A simple list", "•", "- "], ["Numbered list", "A list with numbers", "1.", "1. "],
  ["Quote", "Set text apart", "❝", "> "],
  ["Definition", "A term and what it means", "≝", "::: definition |\n\n:::"], ["Theorem", "A result, stated", "∴", "::: theorem |\n\n:::"],
  ["Example", "A worked example", "e.g.", "::: example |\n\n:::"], ["Tip", "A shortcut or insight", "✦", "::: tip |\n\n:::"],
  ["Note", "An aside", "i", "::: note |\n\n:::"], ["Warning", "A pitfall to remember", "⚠️", "::: warning |\n\n:::"],
  ["Block equation", "LaTeX on its own line", "∑", "$$\n|\n$$"], ["Inline equation", "LaTeX inside a sentence", "x", "$|$"],
  ["Two columns", "Side-by-side content", "▥", "::: columns\n|\n+++\n\n:::"], ["Code", "A block of code; name the language after ```", "{}", "```|\n\n```"],
  ["Table", "Rows and columns", "▦", { table: true }], ["Divider", "A thin line", "—", "---\n"],
  ["Image", "Upload a picture", "▣", { upload: "image" }], ["File", "Attach a file", "⎘", { upload: "file" }],
  ["Component", "A chart or visual Luna makes from your description", "◈", { component: true }],
];

function slash(opts) {
  return (context) => {
    const before = context.matchBefore(/(^|\s)\/[\w ]*$/);
    if (!before) return null;
    const start = before.from + before.text.indexOf("/");
    const query = before.text.slice(before.text.indexOf("/") + 1).toLowerCase();
    if (!context.explicit && query.length > 16) return null;
    return {
      from: start,
      filter: false,
      options: BLOCKS.filter(([label]) => label.toLowerCase().includes(query.trim())).map(([label, detail, glyph, insert], i) => ({
        label, detail, type: "block", boost: -i, glyph,
        apply(view, _c, from, to) {
          if (insert.component) { view.dispatch({ changes: { from, to, insert: "" }, effects: openPrompt.of(from) }); return; }
          if (insert.table) {
            // An empty two-by-two grid on lines of its own, with the cursor in its first cell.
            const line = view.state.doc.lineAt(from), before = view.state.sliceDoc(line.from, from).trim(), after = view.state.sliceDoc(to, line.to).trim();
            const table = tableMarkdown([["", ""], ["", ""]]), text = (before ? "\n\n" : "") + table + (after ? "\n\n" : "");
            view.dispatch({ changes: { from, to, insert: text } });
            const at = from + (before ? 2 : 0);
            requestAnimationFrame(() => { const grid = tableAtPos(view, at); if (grid) focusCell(grid, 0, 0); });
            return;
          }
          if (typeof insert === "object") { view.dispatch({ changes: { from, to, insert: "" } }); opts.pick && opts.pick(insert.upload); return; }
          const caret = insert.indexOf("|"), text = insert.replace("|", "");
          view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + (caret >= 0 ? caret : text.length) } });
        },
      })),
    };
  };
}

// /component: a prompt under the line, then the component Luna writes from it and the document, as a fence there ----

const openPrompt = StateEffect.define(), setPrompt = StateEffect.define(), closePrompt = StateEffect.define();
let prompts = 0;

// The fence a component goes in: longer than any run of backticks inside it, so its code can never close it early.
function componentBlock(prompt, html) {
  const longest = Math.max(2, ...(html.match(/`+/g) || []).map((run) => run.length));
  const fence = "`".repeat(longest + 1), note = prompt.replace(/--+/g, "-").replace(/\s+/g, " ").trim();
  return `${fence}component\n<!-- ${note} -->\n${html.trim()}\n${fence}`;
}

class PromptWidget extends WidgetType {
  constructor(prompt, actions) { super(); this.prompt = prompt; this.actions = actions; }
  eq(o) { return o.prompt === this.prompt; }
  toDOM(view) {
    const wrap = document.createElement("div");
    wrap.className = "md-block md-block-prompt";
    wrap.innerHTML = `<div class="md-prompt"><span class="md-prompt-glyph" aria-hidden="true">◈</span>`
      + `<input class="md-prompt-input" aria-label="Describe the visual" placeholder="Describe the visual">`
      + `<span class="md-prompt-status" role="status"></span></div><p class="md-prompt-error" role="alert"></p>`;
    const input = wrap.querySelector("input");
    input.value = this.prompt.text || "";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); if (input.value.trim()) this.actions.run(view, input.value); }
      else if (e.key === "Escape") { e.preventDefault(); this.actions.cancel(view); }
    });
    this.updateDOM(wrap);
    // CodeMirror attaches the widget before its dispatch returns: focus right after, so typing straight on lands here.
    const focus = () => { if (input.isConnected && !input.disabled && document.activeElement !== input) input.focus(); };
    queueMicrotask(focus);
    requestAnimationFrame(focus);
    return wrap;
  }
  updateDOM(dom) {
    const { status, error } = this.prompt, input = dom.querySelector("input");
    dom.classList.toggle("is-working", status === "working");
    dom.classList.toggle("is-failed", status === "failed");
    input.disabled = status === "working";
    dom.querySelector(".md-prompt-status").textContent = status === "working" ? "Making it…" : "";
    dom.querySelector(".md-prompt-error").textContent = status === "failed" ? error : "";
    // Back to the field once, when it fails, so a retry is one edit away; later redraws leave focus where you put it.
    if (status === "failed" && dom.__status !== "failed") requestAnimationFrame(() => input.isConnected && input.focus());
    dom.__status = status;
    return true;
  }
  get estimatedHeight() { return 52; }
  ignoreEvent() { return true; }
}

function componentPrompt(generate) {
  // Ask for the component, then put it where the prompt was (positions follow edits made while it was working).
  const run = async (view, text) => {
    const prompt = view.state.field(field, false);
    if (!prompt || prompt.status === "working") return;
    const id = prompt.id, abort = new AbortController();
    view.dispatch({ effects: setPrompt.of({ id, status: "working", error: "", text, abort }) });
    let html;
    try {
      if (!generate) throw new Error("Components aren't available here.");
      html = await generate(text.trim(), view.state.doc.toString(), abort.signal);
    } catch (error) {
      if (abort.signal.aborted) return;
      const current = view.state.field(field, false);
      if (current && current.id === id) view.dispatch({ effects: setPrompt.of({ id, status: "failed", error: error.message || "That didn't work." }) });
      return;
    }
    const current = view.state.field(field, false);
    if (!current || current.id !== id) return;  // closed meanwhile
    // On an empty line the component takes the line; after text it starts on the next one.
    const line = view.state.doc.lineAt(current.pos), block = componentBlock(text, html);
    const at = line.text.trim() ? line.to : line.from, insert = line.text.trim() ? `\n${block}` : block;
    const after = line.to === view.state.doc.length ? "\n" : "";
    view.dispatch({ changes: { from: at, to: line.to, insert: insert + after },
      selection: { anchor: at + insert.length + after.length }, effects: closePrompt.of(null), scrollIntoView: true });
    if (view.state.field(mode) === "edit") view.focus();
  };
  const cancel = (view) => {
    view.state.field(field, false)?.abort?.abort();
    view.dispatch({ effects: closePrompt.of(null) });
    view.focus();
  };
  const actions = { run, cancel };
  const field = StateField.define({
    create: () => null,
    update(value, tr) {
      if (value && tr.docChanged) value = { ...value, pos: tr.changes.mapPos(value.pos) };
      for (const e of tr.effects) {
        if (e.is(openPrompt)) { value?.abort?.abort(); value = { id: ++prompts, pos: e.value, status: "idle", error: "", text: "" }; }
        else if (e.is(setPrompt) && value && value.id === e.value.id) value = { ...value, ...e.value };
        else if (e.is(closePrompt)) value = null;
        else if (e.is(setMode) && value && value.status !== "working") value = null;
      }
      return value;
    },
    // Under the line the prompt was opened on.
    provide: (f) => EditorView.decorations.compute([f], (state) => {
      const value = state.field(f);
      if (!value) return Decoration.none;
      const at = state.doc.lineAt(Math.min(value.pos, state.doc.length)).to;
      return Decoration.set([Decoration.widget({ widget: new PromptWidget(value, actions), block: true, side: 1 }).range(at)]);
    }),
  });
  return field;
}

// Pasting -------------------------------------------------------------------------------------------------------------

// A table as Markdown, its columns padded to line up in Edit mode's monospace. `aligns`: "left" | "center" | "right" | "".
function tableMarkdown(rows, aligns = []) {
  const width = Math.max(...rows.map((r) => r.length));
  rows = rows.map((r) => [...r, ...Array(width - r.length).fill("")]);
  const widths = Array.from({ length: width }, (_, i) => Math.max(3, ...rows.map((r) => r[i].length)));
  const row = (r) => `| ${r.map((c, i) => c.padEnd(widths[i])).join(" | ")} |`;
  const rule = widths.map((w, i) => {
    const align = aligns[i];
    return align === "center" ? `:${"-".repeat(w - 2)}:` : align === "right" ? `${"-".repeat(w - 1)}:` : align === "left" ? `:${"-".repeat(w - 1)}` : "-".repeat(w);
  });
  return [row(rows[0]), `| ${rule.join(" | ")} |`, ...rows.slice(1).map(row)].join("\n");
}
const alignOf = (cell) => (/^:-+:$/.test(cell) ? "center" : /-:$/.test(cell) ? "right" : /^:-/.test(cell) ? "left" : "");

// Markdown tables in pasted text (as Claude's and ChatGPT's copy buttons give them), lined up. Fences are left alone.
function alignTables(text) {
  const lines = text.split("\n"), out = [];
  let fence = null;
  for (let i = 0; i < lines.length; i++) {
    const o = FENCE.exec(lines[i]);
    if (fence) { if (o && o[1][0] === fence[0] && o[1].length >= fence.length && !o[2].trim()) fence = null; out.push(lines[i]); continue; }
    if (o) { fence = o[1]; out.push(lines[i]); continue; }
    if (i + 1 < lines.length && tableAt(lines[i], lines[i + 1])) {
      let end = i + 1;
      while (end + 1 < lines.length && tableRow(lines[end + 1])) end++;
      const indent = /^\s*/.exec(lines[i])[0];
      const rows = [lines[i], ...lines.slice(i + 2, end + 1)].map(cellsOf);
      out.push(...tableMarkdown(rows, cellsOf(lines[i + 1]).map(alignOf)).split("\n").map((l) => indent + l));
      i = end;
    } else out.push(lines[i]);
  }
  return out.join("\n");
}

// Cells copied from a spreadsheet arrive tab-separated: every row the same number of tabs.
function tabTable(text) {
  const rows = text.replace(/\r/g, "").replace(/\n+$/, "").split("\n");
  if (rows.length < 2) return null;
  const tabs = rows[0].split("\t").length;
  if (tabs < 2 || rows.some((r) => r.split("\t").length !== tabs) || rows.every((r) => r.startsWith("\t"))) return null;
  return tableMarkdown(rows.map((r) => r.split("\t").map((c) => c.trim().replace(/\|/g, "\\|"))));
}

// Rich text with a table in it (a chat reply, a web page, a spreadsheet) arrives as HTML: it becomes Markdown, the table
// included, keeping headings, lists, emphasis, links, code and maths (KaTeX keeps its TeX source in an annotation).
const BLOCK_TAGS = /^(P|DIV|H[1-6]|UL|OL|PRE|BLOCKQUOTE|TABLE|HR|SECTION|ARTICLE|HEADER|FOOTER|FIGURE|MAIN|DETAILS)$/;
function htmlToMarkdown(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script, style, meta, link, title, button, svg, template, noscript").forEach((n) => n.remove());
  const isBlock = (n) => n.nodeType === 1 && BLOCK_TAGS.test(n.tagName) && !n.classList.contains("katex-display");
  const teX = (el) => el.querySelector('annotation[encoding="application/x-tex"]')?.textContent.trim();
  function inline(node) {
    if (node.nodeType === 3) return node.nodeValue.replace(/\s+/g, " ");
    if (node.nodeType !== 1) return "";
    const el = node, tag = el.tagName;
    if (el.classList.contains("katex-display") && teX(el)) return `$$${teX(el)}$$`;
    if (el.classList.contains("katex") && teX(el)) return `$${teX(el)}$`;
    const inner = () => [...el.childNodes].map(inline).join("");
    // Marks hug the text; spaces at the edges stay outside them.
    const wrap = (m) => { const s = inner(), t = s.trim(); return t ? s.replace(t, () => m + t + m) : s; };
    switch (tag) {
      case "STRONG": case "B": return wrap("**");
      case "EM": case "I": return wrap("*");
      case "U": return wrap("__");
      case "S": case "DEL": case "STRIKE": return wrap("~~");
      case "CODE": return el.textContent ? `\`${el.textContent}\`` : "";
      case "BR": return "\n";
      case "A": {
        const href = el.getAttribute("href") || "", text = inner().trim();
        return /^(https?:|mailto:)/i.test(href) && text ? `[${text}](${href})` : inner();
      }
      case "IMG": {
        const src = el.getAttribute("src") || "";
        return /^https?:/i.test(src) ? `![${el.getAttribute("alt") || ""}](${src})` : "";
      }
      default: return inner();
    }
  }
  function blocks(el) {
    const parts = [];
    let run = "";
    const flush = () => { const t = run.replace(/[ \t]*\n[ \t]*/g, "\n").trim(); if (t) parts.push(t); run = ""; };
    for (const child of el.childNodes) {
      if (isBlock(child)) { flush(); const b = block(child); if (b) parts.push(b); } else run += inline(child);
    }
    flush();
    return parts.join("\n\n");
  }
  function list(el) {
    let n = +(el.getAttribute("start") || 1);
    return [...el.children].filter((li) => li.tagName === "LI").map((li) => {
      const box = li.querySelector(":scope > input[type=checkbox], :scope > p > input[type=checkbox]");
      const marker = el.tagName === "OL" ? `${n++}. ` : box ? `- [${box.checked ? "x" : " "}] ` : "- ";
      const body = blocks(li).replace(/\n{2,}/g, "\n");
      return marker + body.split("\n").map((l, i) => (i ? " ".repeat(el.tagName === "OL" ? marker.length : 2) + l : l)).join("\n");
    }).join("\n");
  }
  function table(el) {
    const rows = [...el.querySelectorAll("tr")].filter((tr) => tr.closest("table") === el)
      .map((tr) => [...tr.children].filter((c) => c.tagName === "TH" || c.tagName === "TD"));
    if (!rows.length) return "";
    const text = rows.map((r) => r.map((c) => inline(c).replace(/\s+/g, " ").trim().replace(/(?<!\\)\|/g, "\\|")));
    const aligns = rows[0].map((c) => (c.getAttribute("align") || c.style.textAlign || "").toLowerCase());
    return tableMarkdown(text, aligns.map((a) => (["left", "center", "right"].includes(a) ? a : "")));
  }
  function block(el) {
    const tag = el.tagName;
    if (/^H[1-6]$/.test(tag)) return `${"#".repeat(+tag[1])} ${inline(el).replace(/\s+/g, " ").trim()}`;
    if (tag === "UL" || tag === "OL") return list(el);
    if (tag === "TABLE") return table(el);
    if (tag === "HR") return "---";
    if (tag === "PRE") {
      const code = el.querySelector("code") || el, lang = (/language-([\w+#.-]+)/.exec(code.className) || [])[1] || "";
      return `\`\`\`${lang}\n${code.textContent.replace(/\n$/, "")}\n\`\`\``;
    }
    if (tag === "BLOCKQUOTE") return blocks(el).split("\n").map((l) => (l ? `> ${l}` : ">")).join("\n");
    return blocks(el);
  }
  return blocks(doc.body).replace(/\n{3,}/g, "\n\n").trim();
}

// What a paste becomes, or null to let CodeMirror paste the plain text as it is. Inside code nothing is converted.
function pastedMarkdown(state, data) {
  if (!data || state.readOnly || inCode(state, state.selection.main.from)) return null;
  const html = data.getData("text/html"), plain = data.getData("text/plain");
  if (html && /<table[\s>]/i.test(html)) { const md = htmlToMarkdown(html); if (md) return md; }
  if (!plain) return null;
  const tabbed = tabTable(plain);
  if (tabbed) return tabbed;
  const aligned = alignTables(plain.replace(/\r/g, ""));
  return aligned !== plain ? aligned : null;
}

// A table needs lines of its own: a blank line before it when it lands after text, one after it when text follows.
function insertPaste(view, text) {
  const { state } = view, r = state.selection.main, start = state.doc.lineAt(r.from), stop = state.doc.lineAt(r.to);
  const before = state.sliceDoc(start.from, r.from), after = state.sliceDoc(r.to, stop.to);
  const lines = text.split("\n"), opens = lines.length > 1 && tableAt(lines[0], lines[1]);
  const closes = lines.length > 1 && lines[lines.length - 1].includes("|");
  const above = start.number > 1 && state.doc.line(start.number - 1).text.trim();
  const insert = (opens && before.trim() ? "\n\n" : opens && above && !before ? "\n" : "") + text + (closes && after.trim() ? "\n\n" : "");
  view.dispatch({ changes: { from: r.from, to: r.to, insert }, selection: { anchor: r.from + insert.length },
    userEvent: "input.paste", scrollIntoView: true });
}

// Files ---------------------------------------------------------------------------------------------------------------

async function insertFiles(view, files, upload, at) {
  for (const file of files) {
    try {
      const { path, name } = await upload(file);
      const text = file.type.startsWith("image/") ? `![${name}](${path})` : `[${name}](${path})`;
      const pos = at ?? view.state.selection.main.head;
      const line = view.state.doc.lineAt(pos);
      const insert = (line.text.trim() ? "\n" : "") + text + "\n";
      view.dispatch({ changes: { from: line.text.trim() ? line.to : line.from, insert }, scrollIntoView: true });
    } catch (e) { console.warn(e); }
  }
}

// Public API -------------------------------------------------------------------------------------------------------

export function createEditor(parent, opts = {}) {
  const resolve = opts.resolve || ((p) => p);
  const field = blockField(resolve);
  const preview = ViewPlugin.fromClass(class {
    constructor(view) { this.decorations = inline(view, field, resolve); }
    update(u) {
      if (u.docChanged || u.viewportChanged || u.selectionSet || u.transactions.some(modeChanged)) this.decorations = inline(u.view, field, resolve);
    }
  }, { decorations: (v) => v.decorations });
  let api;
  const pick = (kind) => {
    const input = document.createElement("input");
    input.type = "file";
    if (kind === "image") input.accept = "image/*";
    input.onchange = () => input.files.length && opts.upload && insertFiles(view, [...input.files], opts.upload);
    input.click();
  };
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: opts.doc || "",
      extensions: [
        mode.init(() => (opts.mode === "edit" ? "edit" : "read")),
        EditorView.editable.from(mode, (m) => m === "edit"), EditorState.readOnly.from(mode, (m) => m !== "edit"),
        focused, EditorView.focusChangeEffect.of((_, focusing) => setFocus.of(focusing)), field, preview, mathTip, closeFence,
        componentPrompt(opts.generate),
        Prec.high(keymap.of([...FORMAT_KEYS, { key: "ArrowDown", run: enterBlock(field, 1) }, { key: "ArrowUp", run: enterBlock(field, -1) },
          { key: "ArrowLeft", run: enterMath(-1) }, { key: "ArrowRight", run: enterMath(1) }])),
        wrapSelection, closeBrackets(), pairs,
        history(), drawSelection(), EditorView.lineWrapping,
        markdown({ base: markdownLanguage, extensions: [InlineMath, Underline], codeLanguages: (info) => language(info) }),
        syntaxHighlighting(highlight), syntaxHighlighting(classHighlighter),
        placeholderExt(opts.placeholder || "Write, or type / for blocks"),
        autocompletion({ override: [slash({ pick }), latex(field)], icons: false, defaultKeymap: true, closeOnBlur: true,
          addToOptions: [{ render: glyph, position: 20 }] }),
        keymap.of([...closeBracketsKeymap, ...completionKeymap, ...markdownKeymap, ...defaultKeymap, ...historyKeymap, indentWithTab]),
        EditorView.updateListener.of((u) => {
          if (u.focusChanged && !u.view.hasFocus && opts.onBlur) opts.onBlur();
          if (u.docChanged && opts.onChange && !api.silent) opts.onChange(u.state.doc.toString());
        }),
        EditorView.domEventHandlers({
          paste(e, v) {
            // Text first: a spreadsheet copies a picture of its cells beside the cells themselves.
            const md = pastedMarkdown(v.state, e.clipboardData);
            if (md != null) { e.preventDefault(); insertPaste(v, md); return true; }
            const files = [...(e.clipboardData?.files || [])];
            if (!files.length || !opts.upload) return false;
            e.preventDefault(); insertFiles(v, files, opts.upload); return true;
          },
          drop(e, v) {
            const files = [...(e.dataTransfer?.files || [])];
            if (!files.length || !opts.upload) return false;
            e.preventDefault();
            insertFiles(v, files, opts.upload, v.posAtCoords({ x: e.clientX, y: e.clientY }) ?? undefined);
            return true;
          },
        }),
        EditorView.contentAttributes.of({ "aria-label": opts.label || "Document", spellcheck: "true" }),
      ],
    }),
  });
  // The page scrolls the editor inside its own container (title, header and text move together). CodeMirror doesn't see
  // that scroll, so ↑/↓ and clicks would aim at stale coordinates and skip lines: re-measure whenever an ancestor scrolls.
  const scrollers = [];
  const watch = () => {
    if (scrollers.length) return;
    for (let el = view.dom.parentElement; el; el = el.parentElement) {
      if (/(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY)) {
        const onScroll = () => view.requestMeasure();
        el.addEventListener("scroll", onScroll, { passive: true });
        scrollers.push([el, onScroll]);
      }
    }
  };
  requestAnimationFrame(watch);  // the host is in the page by the next frame
  view.contentDOM.addEventListener("focus", watch);
  // CodeMirror ignores its own resize events for 75 ms after an update. Switching modes changes the page's width (the
  // writing layout) in that window, so the caret and line heights went stale (text drawn over text, clicks on the wrong
  // line). Re-measure on every size change ourselves, and when KaTeX's fonts arrive and reshape the maths.
  const resized = new ResizeObserver(() => view.requestMeasure());
  resized.observe(view.dom);
  const fontsLoaded = () => view.requestMeasure();
  document.fonts?.addEventListener("loadingdone", fontsLoaded);
  // The line at the top of the page, and how far above the page's top edge it starts, so a mode switch can put it back.
  const scroller = () => (scrollers.length ? scrollers[0][0] : null);
  const topLine = () => {
    const el = scroller(), top = el ? el.getBoundingClientRect().top : 0, y = Math.max(0, top - view.documentTop);
    let block = view.lineBlockAtHeight(y);
    // The first line wholly on screen: one cut off at the top edge (or a tall block mostly scrolled past) anchors badly.
    if (block.top < y - 1 && block.to < view.state.doc.length) block = view.lineBlockAt(block.to + 1);
    return { pos: block.from, offset: view.documentTop + block.top - top };
  };
  // Right-click menus come from the page (the dashboard's own menu), so tables can offer row and column edits.
  view.dom.__lunaMenu = opts.contextMenu;
  api = {
    view,
    silent: false,
    get mode() { return view.state.field(mode); },
    // The line at the top of the page now; take it before anything changes the page's width.
    anchor() { watch(); return topLine(); },
    // Read ⇄ Edit. The line at the top of the page (`anchor`, taken before the page changed) stays where it was, so the
    // switch reads as the text changing form in place. Entering Edit puts the cursor on that line unless it's already
    // somewhere on screen.
    setMode(next, anchor = api.anchor()) {
      if (next === view.state.field(mode)) return;
      const el = scroller();
      view.dispatch({ effects: setMode.of(next) });
      // The other mode is a different height, so the anchor line may not be drawn yet: aim by CodeMirror's height map
      // first, then correct from the drawn line once it has measured (twice, as estimates settle).
      const align = () => {
        if (!el) return;
        const drawnAt = view.coordsAtPos(anchor.pos, 1);
        const y = drawnAt ? drawnAt.top : view.documentTop + view.lineBlockAt(anchor.pos).top;
        const delta = y - (el.getBoundingClientRect().top + anchor.offset);
        if (Math.abs(delta) >= 1) el.scrollTop += delta;
      };
      align();
      const settle = (n) => view.requestMeasure({ read: () => null, write: () => { align(); if (n) settle(n - 1); } });
      settle(1);
      if (next === "edit") {
        // Focus first: WebKit puts the DOM selection at the start when an element becomes focusable and focused, and
        // CodeMirror only writes its own selection to the DOM while it has focus.
        const head = view.coordsAtPos(view.state.selection.main.head), box = el ? el.getBoundingClientRect() : null;
        const keep = head && !(box && (head.top < box.top || head.bottom > box.bottom)) ? view.state.selection.main.head : anchor.pos;
        const scrollTop = el && el.scrollTop;
        view.contentDOM.focus({ preventScroll: true });
        view.dispatch({ selection: { anchor: keep } });
        if (el && el.scrollTop !== scrollTop) el.scrollTop = scrollTop;
      } else view.contentDOM.blur();
    },
    // Replace the text without firing onChange (a newer version arrived from elsewhere), keeping the cursor if it fits.
    setDoc(text) {
      if (text === view.state.doc.toString()) return;
      api.silent = true;
      const head = Math.min(view.state.selection.main.head, text.length);
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, selection: { anchor: head } });
      api.silent = false;
    },
    focus(end) { view.focus(); if (end) view.dispatch({ selection: { anchor: view.state.doc.length } }); },

    slash() { view.focus(); view.dispatch(view.state.replaceSelection("/")); startCompletion(view); },
    destroy() {
      for (const [el, fn] of scrollers) el.removeEventListener("scroll", fn);
      resized.disconnect();
      document.fonts?.removeEventListener("loadingdone", fontsLoaded);
      view.destroy();
    },
  };
  return api;
}

export { render as renderMarkdown };
