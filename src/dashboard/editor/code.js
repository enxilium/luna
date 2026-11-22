// Code blocks: which language a fence names, what to call it, and highlighting outside the editor. The same Lezer
// parsers highlight a block while you edit it (nested in the Markdown tree) and when it's rendered, so both read alike.

import { StreamLanguage } from "@codemirror/language";
import { highlightCode, classHighlighter } from "@lezer/highlight";
import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { cpp } from "@codemirror/lang-cpp";
import { sql, PostgreSQL } from "@codemirror/lang-sql";
import { java } from "@codemirror/lang-java";
import { rust } from "@codemirror/lang-rust";
import { go } from "@codemirror/lang-go";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { haskell } from "@codemirror/legacy-modes/mode/haskell";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { yaml } from "@codemirror/legacy-modes/mode/yaml";

// Built on first use: most notes never open half of these.
const LANGUAGES = {
  python: ["Python", () => python().language],
  javascript: ["JavaScript", () => javascript().language],
  jsx: ["JSX", () => javascript({ jsx: true }).language],
  typescript: ["TypeScript", () => javascript({ typescript: true }).language],
  tsx: ["TSX", () => javascript({ jsx: true, typescript: true }).language],
  c: ["C", () => cpp().language],
  cpp: ["C++", () => cpp().language],
  sql: ["SQL", () => sql({ dialect: PostgreSQL }).language],  // course SQL is PostgreSQL
  java: ["Java", () => java().language],
  rust: ["Rust", () => rust().language],
  go: ["Go", () => go().language],
  html: ["HTML", () => html().language],
  css: ["CSS", () => css().language],
  json: ["JSON", () => json().language],
  shell: ["Shell", () => StreamLanguage.define(shell)],
  haskell: ["Haskell", () => StreamLanguage.define(haskell)],
  latex: ["LaTeX", () => StreamLanguage.define(stex)],
  yaml: ["YAML", () => StreamLanguage.define(yaml)],
};
const ALIASES = { py: "python", python3: "python", js: "javascript", mjs: "javascript", node: "javascript", ts: "typescript",
  h: "c", "c++": "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", postgres: "sql", postgresql: "sql", psql: "sql", plpgsql: "sql",
  mysql: "sql", sqlite: "sql", rs: "rust", golang: "go", htm: "html", xml: "html", svg: "html", jsonc: "json",
  sh: "shell", bash: "shell", zsh: "shell", console: "shell", hs: "haskell", tex: "latex", yml: "yaml" };
const built = new Map();

// The fence's info string ("py", "SQL", "c++ title=x") to a known language key, or null.
export function languageKey(info) {
  const word = String(info || "").trim().split(/\s+/)[0].toLowerCase();
  const key = ALIASES[word] || word;
  return LANGUAGES[key] ? key : null;
}

export function language(info) {
  const key = languageKey(info);
  if (!key) return null;
  if (!built.has(key)) built.set(key, LANGUAGES[key][1]());
  return built.get(key);
}

// What the block's label says: the language's proper name, or the fence's own word for one we don't parse.
export function languageName(info) {
  const key = languageKey(info);
  const word = String(info || "").trim().split(/\s+/)[0];
  return key ? LANGUAGES[key][0] : word;
}

const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// Highlighted HTML (tok-* classes, the same ones the editor uses) for a block shown rendered.
export function highlight(code, info) {
  const lang = language(info);
  if (!lang) return escape(code);
  let out = "";
  highlightCode(code, lang.parser.parse(code), classHighlighter,
    (text, classes) => { out += classes ? `<span class="${classes}">${escape(text)}</span>` : escape(text); },
    () => { out += "\n"; });
  return out;
}

// A rendered code block: the language on top, a copy button, then the code.
export function codeBlockHtml(code, info) {
  const name = languageName(info);
  return `<div class="md-pre" data-lang="${escape(languageKey(info) || "")}">`
    + `<div class="md-pre-head"><span class="md-pre-lang">${escape(name)}</span>`
    + `<button type="button" class="md-copy" aria-label="Copy code">Copy</button></div>`
    + `<pre><code>${highlight(code, info)}</code></pre></div>`;
}

export { classHighlighter };
