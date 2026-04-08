"use strict";

// ── Language detection ─────────────────────────────────────────────────────────
const { EditorView, EditorState, basicSetup, keymap, indentWithTab, oneDark, langs } = window.CM;

function detectLang(ext) {
  const map = {
    py:         langs.python,
    js:         () => langs.javascript(),
    jsx:        () => langs.javascript({ jsx: true }),
    ts:         () => langs.javascript({ typescript: true }),
    tsx:        () => langs.javascript({ jsx: true, typescript: true }),
    html:       langs.html,
    htm:        langs.html,
    css:        langs.css,
    scss:       langs.css,
    less:       langs.css,
    json:       langs.json,
    yaml:       langs.yaml,
    yml:        langs.yaml,
    md:         langs.markdown,
    markdown:   langs.markdown,
    xml:        langs.xml,
    svg:        langs.xml,
    sql:        langs.sql,
    sh:         langs.shell,
    bash:       langs.shell,
    zsh:        langs.shell,
    fish:       langs.shell,
    toml:       langs.toml,
    dockerfile: langs.dockerfile,
    nginx:      langs.nginx,
  };
  const factory = map[ext];
  if (!factory) return [];
  try { return [factory()]; } catch { return []; }
}

// ── Editor setup ───────────────────────────────────────────────────────────────
const langExtension = detectLang(FILE_EXT);

const view = new EditorView({
  state: EditorState.create({
    doc: CONTENT,
    extensions: [
      basicSetup,
      keymap.of([indentWithTab]),
      oneDark,
      ...langExtension,
      EditorView.lineWrapping,
    ],
  }),
  parent: document.getElementById("editor-wrap"),
});

// ── Save ───────────────────────────────────────────────────────────────────────
const saveStatus = document.getElementById("save-status");
let saveTimer = null;

function setStatus(cls, msg) {
  saveStatus.className = "save-status " + cls;
  saveStatus.textContent = msg;
}

async function save() {
  setStatus("saving", "saving…");
  const content = view.state.doc.toString();
  try {
    const res = await fetch(FILE_PATH, {
      method: "PUT",
      body: content,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
    if (!res.ok) throw new Error(res.status);
    setStatus("saved", "saved");
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => setStatus("", ""), 3000);
  } catch (e) {
    setStatus("error", "save failed: " + e.message);
  }
}

document.getElementById("save-btn").addEventListener("click", save);

// Ctrl+S / Cmd+S
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    save();
  }
});

// ── Unsaved changes guard ──────────────────────────────────────────────────────
let dirty = false;
view.dispatch({ effects: [] });  // init

const originalContent = CONTENT;
document.addEventListener("keydown", () => {
  dirty = view.state.doc.toString() !== originalContent;
});

window.addEventListener("beforeunload", e => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
