// Luna's Markdown editor: one CodeMirror 6 view over plain Markdown with two modes, switched on purpose (⌘E), never by
// where the cursor is. Read renders everything (maths with KaTeX, images, code blocks, callouts, columns) and isn't
// editable, though task boxes still click. Edit shows the source, styled, and nothing in it changes shape as the cursor
// moves: code, callouts and display maths keep their frames, every equation keeps its rendering underneath, and inline
// maths previews in a floating tip. Notes and task descriptions share it; only their headers differ.

import { EditorState, EditorSelection, StateField, StateEffect, Prec } from "@codemirror/state";
import { EditorView, Decoration, ViewPlugin, WidgetType, keymap, placeholder as placeholderExt, drawSelection, showTooltip } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { markdown, markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
import { syntaxTree, syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { autocompletion, completionKeymap, startCompletion, snippetCompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { tags as t } from "@lezer/highlight";
import katex from "katex";
import { Marked } from "marked";
import { language, codeBlockHtml, classHighlighter } from "./code.js";

// Rendered blocks' code fences get the same highlighting and frame as the editor's own.
const marked = new Marked({ gfm: true, breaks: false, renderer: { code: ({ text, lang }) => codeBlockHtml(text, lang) } });

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
  toDOM() {
    const el = document.createElement(this.display ? "div" : "span");
    el.className = this.display ? `md-math md-math-block${this.preview ? " is-preview" : ""}` : `md-math${this.wide ? " is-wide" : ""}`;
    el.innerHTML = tex(this.source, this.display || this.wide);
    if (!this.display) return el;
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
// (::: columns … +++ … :::) and callouts (::: kind Title … :::). Nothing inside a code fence starts another block.
const FENCE = /^\s*(`{3,}|~{3,})\s*([^`]*)$/;
const CONTAINER = /^:::\s*[a-z]+(\s|$)/i;

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
      out.push({ kind: "code", info: fence[2].trim(), code: lines(n + 1, end - 1), from: line.from, to: doc.line(end).to, lines: [n, end] });
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
    }
  }
  // Containers' lines are scanned too, so Edit mode can frame and preview what's inside them; `within` names the
  // container a block sits in (Read mode renders it as part of that container).
  for (const b of out) b.within = out.find((c) => c !== b && (c.kind === "columns" || c.kind === "callout") && b.from > c.from && b.to <= c.to);
  return out;
}

// In Edit mode code, callouts and display maths keep a frame around their source lines, whatever the cursor does.
function frame(state, b, set) {
  const [first, last] = b.lines;
  const kind = b.kind === "callout" ? calloutHead(b.type, b.title).kind : null;
  const base = b.kind === "code" ? "md-codeblock" : b.kind === "math" ? "md-mathsrc" : `md-box-src md-box-${kind}`;
  for (let n = first; n <= last; n++) {
    const cls = [base, n === first && "is-first", n === last && "is-last"];
    set.push(Decoration.line({ class: cls.filter(Boolean).join(" ") }).range(state.doc.line(n).from));
  }
  if (b.kind === "callout") {
    const head = state.doc.line(first), m = /^(\s*:::\s*)([a-z]+)/i.exec(head.text);
    if (m) set.push(Decoration.mark({ class: "md-box-type" }).range(head.from + m[1].length, head.from + m[1].length + m[2].length));
  }
}

// Edit mode shows each image under the line that names it (outside code fences).
const IMAGE = /!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g;
function imagesBelow(state, resolve, set) {
  let fence = null;
  for (let n = 1; n <= state.doc.lines; n++) {
    const line = state.doc.line(n), o = FENCE.exec(line.text);
    if (fence) { if (o && o[1][0] === fence[0] && o[1].length >= fence.length && !o[2].trim()) fence = null; continue; }
    if (o) { fence = o[1]; continue; }
    if (!line.text.includes("![")) continue;
    for (const m of line.text.matchAll(IMAGE)) {
      set.push(Decoration.widget({ widget: new ImageWidget(resolve(m[2]), m[1], true), block: true, side: 1 }).range(line.to));
    }
  }
}

function blockField(resolve) {
  const build = (state) => {
    const ranges = blocks(state), set = [];
    for (const b of ranges) {
      if (editing(state)) {
        // A callout's frame is the frame for what's inside it; columns have none, so their contents get their own.
        if (b.kind !== "columns" && !(b.within && b.within.kind === "callout")) frame(state, b, set);
        if (b.kind === "math" && b.source.trim()) set.push(Decoration.widget({ widget: new MathWidget(b.source, true, true), block: true, side: 1 }).range(b.to));
        continue;
      }
      if (b.within) continue;
      const widget = b.kind === "math" ? new MathWidget(b.source, true)
        : b.kind === "code" ? new CodeWidget(b.info, b.code)
        : b.kind === "callout" ? new CalloutWidget(b.type, b.title, b.body, resolve)
        : new ColumnsWidget(b.columns, resolve);
      set.push(Decoration.replace({ widget, block: true }).range(b.from, b.to));
    }
    if (editing(state)) imagesBelow(state, resolve, set);
    return { decorations: Decoration.set(set, true), ranges };
  };
  return StateField.define({
    create: build,
    update: (value, tr) => (tr.docChanged || modeChanged(tr) ? build(tr.state) : value),
    provide: (f) => EditorView.decorations.from(f, (v) => v.decorations),
  });
}

// Inline: both modes style headings and quotes by line and draw task boxes; Read also hides the marks and swaps in maths,
// images, bullets and rules.
const HIDE = new Set(["HeaderMark", "EmphasisMark", "CodeMark", "StrikethroughMark", "QuoteMark"]);
const hidden = Decoration.replace({});

function inline(view, field, resolve) {
  const { state } = view, set = [], edit = editing(state);
  const covered = edit ? [] : state.field(field).ranges;
  // Only nodes wholly inside a rendered block are skipped (the document itself starts inside a leading one).
  const inBlock = (from, to) => covered.some((b) => from >= b.from && to <= b.to);
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
          if (!edit) set.push(hidden.range(at, at + tag[2].length));
          if (tag[4]) set.push(Decoration.mark({ class: "md-callout-title" }).range(at + tag[2].length, first.to));
        }
      } else if (name === "FencedCode") {
        return false;  // framed by the block field; its own language highlights it
      } else if (name === "HorizontalRule" && !edit) set.push(Decoration.replace({ widget: new RuleWidget() }).range(node.from, node.to));
      else if (HIDE.has(name) && !edit) {
        // A heading's mark takes its trailing space with it.
        const end = name === "HeaderMark" && state.doc.sliceString(node.to, node.to + 1) === " " ? node.to + 1 : node.to;
        set.push(hidden.range(node.from, end));
      } else if (name === "ListMark" && !edit) {
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
        if (!edit) {
          const n = mathMarks(state.doc, node);
          set.push(Decoration.replace({ widget: new MathWidget(state.doc.sliceString(node.from + n, node.to - n), false, null, false, n === 2) }).range(node.from, node.to));
        }
        return false;
      } else if (name === "Image") {
        if (!edit) {
          const text = state.doc.sliceString(node.from, node.to), m = /^!\[([^\]]*)\]\(([^)\s]+)/.exec(text);
          if (m) set.push(Decoration.replace({ widget: new ImageWidget(resolve(m[2]), m[1]) }).range(node.from, node.to));
        }
        return false;
      } else if (name === "Link") {
        if (state.doc.sliceString(node.from, node.from + 2) === "[!") return false;  // a callout tag, not a link
        if (!edit) {
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

// ⌘B, ⌘I, ⌘⇧C (code), ⌘⇧S (strikethrough), ⌘⇧E (maths), Notion's keys except code (⌘E switches modes): wrap the selection in the marks, or
// unwrap it when it's already wrapped (marks just outside or at the edges of the selection). With nothing selected the
// marks go in around the cursor. Stars are counted, so ⌘I inside **bold** adds italics rather than eating a star.
const present = (run, mark) => (mark === "**" ? run >= 2 : mark === "*" ? run % 2 === 1 : run >= mark.length);
function runOf(doc, pos, ch, dir) {
  let n = 0;
  while (n < 3 && doc.sliceString(dir < 0 ? pos - n - 1 : pos + n, dir < 0 ? pos - n : pos + n + 1) === ch) n++;
  return n;
}
function toggle(mark) {
  return (view) => {
    const { state } = view, n = mark.length, ch = mark[0], doc = state.doc;
    view.dispatch(state.changeByRange(({ from, to }) => {
      if (present(Math.min(runOf(doc, from, ch, -1), runOf(doc, to, ch, 1)), mark)) {
        return { changes: [{ from: from - n, to: from }, { from: to, to: to + n }], range: EditorSelection.range(from - n, to - n) };
      }
      if (to - from > 2 * n && present(Math.min(runOf(doc, from, ch, 1), runOf(doc, to, ch, -1)), mark)) {
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
  { key: "Mod-b", run: toggle("**") }, { key: "Mod-i", run: toggle("*") }, { key: "Mod-Shift-c", run: toggle("`") },
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
  ["Table", "Rows and columns", "▦", "| Column | Column |\n| --- | --- |\n| | |"], ["Divider", "A thin line", "—", "---\n"],
  ["Image", "Upload a picture", "▣", { upload: "image" }], ["File", "Attach a file", "⎘", { upload: "file" }],
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
          if (typeof insert === "object") { view.dispatch({ changes: { from, to, insert: "" } }); opts.pick && opts.pick(insert.upload); return; }
          const caret = insert.indexOf("|"), text = insert.replace("|", "");
          view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + (caret >= 0 ? caret : text.length) } });
        },
      })),
    };
  };
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
      if (u.docChanged || u.viewportChanged || u.transactions.some(modeChanged)) this.decorations = inline(u.view, field, resolve);
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
        Prec.high(keymap.of(FORMAT_KEYS)),
        wrapSelection, closeBrackets(), pairs,
        history(), drawSelection(), EditorView.lineWrapping,
        markdown({ base: markdownLanguage, extensions: [InlineMath], codeLanguages: (info) => language(info) }),
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
