// Bundles editor.js into one ES module for the no-build front end, and copies KaTeX's stylesheet and fonts beside it.
import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

const out = new URL("../web/vendor/", import.meta.url).pathname;
mkdirSync(out, { recursive: true });
await build({ entryPoints: ["editor.js"], bundle: true, format: "esm", minify: true, target: "es2022", outfile: `${out}editor.js`,
  legalComments: "linked" });
cpSync("node_modules/katex/dist/katex.min.css", `${out}katex/katex.min.css`);
cpSync("node_modules/katex/dist/fonts", `${out}katex/fonts`, { recursive: true });
console.log(`Built ${out}editor.js`);
