// Build a self-contained CodeMirror 6 bundle for X-wing's in-browser editor.
// Run with: node bundle-editor.mjs
// Output:   ../xwing/static/codemirror-bundle.js

import { build } from "esbuild";

// Entry point — everything the editor needs, exposed as window.CM
const entry = `
import { EditorView, basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { keymap as cmKeymap } from "@codemirror/view";
import { closeSearchPanel as cmCloseSearchPanel, searchPanelOpen as cmSearchPanelOpen } from "@codemirror/search";
import { indentWithTab as cmIndentWithTab } from "@codemirror/commands";
import { oneDark } from "@codemirror/theme-one-dark";

import { python }     from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { html }       from "@codemirror/lang-html";
import { css }        from "@codemirror/lang-css";
import { json }       from "@codemirror/lang-json";
import { yaml }       from "@codemirror/lang-yaml";
import { markdown }   from "@codemirror/lang-markdown";
import { xml }        from "@codemirror/lang-xml";
import { sql }        from "@codemirror/lang-sql";
import { StreamLanguage } from "@codemirror/language";
import { shell }      from "@codemirror/legacy-modes/mode/shell";
import { toml }       from "@codemirror/legacy-modes/mode/toml";
import { dockerFile } from "@codemirror/legacy-modes/mode/dockerfile";
import { nginx }      from "@codemirror/legacy-modes/mode/nginx";

window.CM = {
  EditorView, EditorState, basicSetup, keymap: cmKeymap, indentWithTab: cmIndentWithTab, oneDark,
  searchPanelOpen: cmSearchPanelOpen, closeSearchPanel: cmCloseSearchPanel,
  langs: {
    python, javascript, html, css, json, yaml, markdown, xml, sql,
    shell: () => StreamLanguage.define(shell),
    toml:  () => StreamLanguage.define(toml),
    dockerfile: () => StreamLanguage.define(dockerFile),
    nginx: () => StreamLanguage.define(nginx),
  },
};
`;

await build({
  stdin: { contents: entry, resolveDir: "./node_modules/.bin/../.." },
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020"],
  outfile: "../xwing/static/codemirror-bundle.js",
  logLevel: "info",
});

console.log("Done -> xwing/static/codemirror-bundle.js");
