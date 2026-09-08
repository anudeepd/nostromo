import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { useModalFocus } from "./keyboard";

interface EditorBootstrap {
  path: string; directory: string; filename: string; displayPath: string;
  extension: string; content: string; truncated: boolean; totalSize: number;
  previewBytes: number;
  user: { name: string; authenticated: boolean };
  canWrite: boolean; maxChunkBytes: number; cspNonce: string; authIdleTimeout: number;
}

interface CodeMirrorView {
  state: { doc: { toString(): string } };
  focus(): void;
  destroy(): void;
}

interface CodeMirrorApi {
  EditorView: {
    new(options: unknown): CodeMirrorView;
    editable: { of(value: boolean): unknown };
    lineWrapping: unknown;
    updateListener: { of(listener: (update: { docChanged: boolean; state: CodeMirrorView["state"] }) => void): unknown };
    cspNonce: { of(value: string): unknown };
  };
  EditorState: { create(options: unknown): unknown; readOnly: { of(value: boolean): unknown } };
  basicSetup: unknown; keymap: { of(value: unknown[]): unknown }; indentWithTab: unknown; oneDark: unknown;
  searchPanelOpen(state: unknown): boolean;
  closeSearchPanel(view: CodeMirrorView): boolean;
  langs: Record<string, (...args: unknown[]) => unknown>;
}

declare global { interface Window { CM: CodeMirrorApi } }

const AUTH_REDIRECT_DELAY_MS = 1500;

// Saves that fit in one chunk go out as a single PUT, exactly like before.
// Larger saves reuse the resumable `/_upload` session API (init → chunk PUTs →
// complete) so a failed save retries one small chunk instead of the whole
// document, and the status line can report real progress. Chunk sizing mirrors
// the file browser's uploader: 8 MB pieces capped by the server limit.
const SAVE_CHUNK_BYTES = 8 * 1024 * 1024;
const SAVE_RETRY_DELAYS_MS = [750, 1500, 3000];
const RETRYABLE_SAVE_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

function saveChunkBytes(boot: EditorBootstrap): number {
  const serverMax = boot.maxChunkBytes > 0 ? boot.maxChunkBytes : SAVE_CHUNK_BYTES;
  return Math.max(1, Math.min(SAVE_CHUNK_BYTES, serverMax));
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

function Logo(): React.JSX.Element {
  return <svg className="brand-mark" viewBox="0 0 200 200" aria-label="X-wing logo"><rect x="6" y="6" width="188" height="188" rx="36"/><g fill="none" strokeLinecap="round" strokeLinejoin="round"><polygon points="71,78 23,48 15,100 23,152 71,122"/><polyline points="71,78 30,100 71,122"/><polygon points="129,78 177,48 185,100 177,152 129,122"/><polyline points="129,78 170,100 129,122"/><path d="m71 78 15 8m-15 36 15-8m43-36-15 8m15 36-15-8"/><circle cx="100" cy="100" r="20"/><circle cx="100" cy="100" r="13"/></g><circle className="brand-core" cx="100" cy="100" r="4.5"/></svg>;
}

function EditorApp({ boot }: { boot: EditorBootstrap }): React.JSX.Element {
  const mount = useRef<HTMLDivElement>(null);
  const view = useRef<CodeMirrorView | null>(null);
  const closingRef = useRef(false);
  const logoutForm = useRef<HTMLFormElement>(null);
  const saved = useRef(boot.content);
  const allowLeave = useRef(false);
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState("");
  const [confirmLeave, setConfirmLeave] = useState<string | null>(null);
  const [authOverlay, setAuthOverlay] = useState<"signout" | "expired" | null>(null);
  const [pageLeaving, setPageLeaving] = useState(false);
  const canEdit = boot.canWrite && !boot.truncated;

  useEffect(() => {
    const cm = window.CM;
    const language = detectLanguage(cm, boot.extension);
    const editor = new cm.EditorView({
      state: cm.EditorState.create({ doc: boot.content, extensions: [
        cm.basicSetup, cm.keymap.of([cm.indentWithTab]), cm.oneDark,
        ...(boot.cspNonce ? [cm.EditorView.cspNonce.of(boot.cspNonce)] : []),
        ...language,
        ...(!canEdit ? [cm.EditorView.editable.of(false), cm.EditorState.readOnly.of(true)] : []),
        cm.EditorView.lineWrapping,
        cm.EditorView.updateListener.of(update => { if (update.docChanged) setDirty(update.state.doc.toString() !== saved.current); }),
      ] }),
      parent: mount.current,
    });
    view.current = editor;
    editor.focus();
    return () => editor.destroy();
  }, []);

  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty && !allowLeave.current) event.preventDefault(); };
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void save(); }
      if (event.key === "Escape" && !event.defaultPrevented && !window.CM.searchPanelOpen(view.current?.state)) {
        if (!confirmLeave) { event.preventDefault(); requestLeave(boot.directory); }
      }
    };
    const onKeydownCapture = (event: KeyboardEvent) => {
      const cm = window.CM;
      const editor = view.current;
      if (!editor) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f" && cm.searchPanelOpen(editor.state)) {
        const field = mount.current?.querySelector<HTMLInputElement>(".cm-panel.cm-search [main-field]");
        if (field) { event.preventDefault(); event.stopPropagation(); field.focus(); field.select(); }
        return;
      }
      if (event.key === "Escape" && closeSearchPanelAnimated()) { event.preventDefault(); event.stopPropagation(); }
    };
    const onCloseClickCapture = (event: MouseEvent) => {
      const target = event.target as Element | null;
      if (!target?.closest?.(".cm-panel.cm-search [name=close]")) return;
      if (closeSearchPanelAnimated()) { event.preventDefault(); event.stopPropagation(); }
    };
    window.addEventListener("beforeunload", beforeUnload); document.addEventListener("keydown", shortcut);
    document.addEventListener("keydown", onKeydownCapture, true);
    document.addEventListener("click", onCloseClickCapture, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("keydown", shortcut);
      document.removeEventListener("keydown", onKeydownCapture, true);
      document.removeEventListener("click", onCloseClickCapture, true);
    };
  }, [dirty, confirmLeave]);

  function closeSearchPanelAnimated(): boolean {
    const cm = window.CM;
    const editor = view.current;
    if (!editor || closingRef.current || !cm.searchPanelOpen(editor.state)) return false;
    closingRef.current = true;
    const panel = mount.current?.querySelector(".cm-panel.cm-search");
    if (panel && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      panel.classList.add("closing");
      window.setTimeout(() => {
        cm.closeSearchPanel(editor);
        closingRef.current = false;
      }, 160);
    } else {
      cm.closeSearchPanel(editor);
      closingRef.current = false;
    }
    return true;
  }

  useEffect(() => {
    if (!boot.authIdleTimeout) return;
    let deadline = Date.now() + boot.authIdleTimeout * 1000;
    let timer = window.setTimeout(expire, boot.authIdleTimeout * 1000);
    function expire(): void {
      if (Date.now() < deadline) { timer = window.setTimeout(expire, deadline - Date.now()); return; }
      setAuthOverlay("expired");
      window.setTimeout(() => location.assign(loginUrl()), AUTH_REDIRECT_DELAY_MS);
    }
    function activity(): void { deadline = Date.now() + boot.authIdleTimeout * 1000; window.clearTimeout(timer); timer = window.setTimeout(expire, boot.authIdleTimeout * 1000); }
    const events = ["pointerdown", "keydown", "touchstart", "wheel"] as const;
    events.forEach(name => window.addEventListener(name, activity, { passive: true }));
    return () => { window.clearTimeout(timer); events.forEach(name => window.removeEventListener(name, activity)); };
  }, []);

  const requireAuthOk = (response: Response): void => {
    if (response.status === 401 || new URL(response.url || location.href, location.href).pathname === "/_auth/login") {
      setAuthOverlay("expired"); window.setTimeout(() => location.assign(loginUrl()), AUTH_REDIRECT_DELAY_MS); throw new Error("authentication required");
    }
  };

  /** PUT with retries on network errors and retryable statuses, mirroring the file uploader. */
  const putWithSaveRetries = async (url: string, body: BodyInit, label: string): Promise<Response> => {
    // Trailing `undefined` entry is the final attempt: no further sleep after it.
    for (const delay of [...SAVE_RETRY_DELAYS_MS, undefined]) {
      try {
        const response = await fetch(url, { method: "PUT", body });
        if (delay !== undefined && RETRYABLE_SAVE_STATUSES.has(response.status)) {
          await sleep(delay);
          continue;
        }
        return response;
      } catch (error) {
        if (delay === undefined) throw error;
        await sleep(delay);
      }
    }
    throw new Error(`${label} failed`);
  };

  /** Save a large document through a resumable upload session (atomic replace on complete). */
  const saveChunked = async (blob: Blob, chunkSize: number): Promise<void> => {
    const totalChunks = Math.max(1, Math.ceil(blob.size / chunkSize));
    const initResponse = await fetch("/_upload/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: boot.filename, total_chunks: totalChunks, dir: boot.directory }),
    });
    requireAuthOk(initResponse);
    if (!initResponse.ok) throw new Error(`Save failed (${initResponse.status})`);
    const initData = (await initResponse.json()) as { session_id?: string };
    if (!initData.session_id) throw new Error("Save failed (no upload session)");
    const sessionId = initData.session_id;
    for (let index = 0; index < totalChunks; index += 1) {
      const chunk = blob.slice(index * chunkSize, Math.min(blob.size, (index + 1) * chunkSize));
      const response = await putWithSaveRetries(`/_upload/${sessionId}/${index}`, chunk, `Chunk ${index + 1}/${totalChunks}`);
      requireAuthOk(response);
      if (!response.ok) throw new Error(`Save failed (${response.status})`);
      setStatus(`Saving… ${Math.round(((index + 1) / totalChunks) * 100)}%`);
    }
    const completeResponse = await fetch(`/_upload/${sessionId}/complete`, { method: "POST" });
    requireAuthOk(completeResponse);
    if (!completeResponse.ok) throw new Error(`Save failed (${completeResponse.status})`);
  };

  const save = async (): Promise<void> => {
    if (!canEdit || !view.current) return;
    const content = view.current.state.doc.toString();
    // Blob slices are lazy views: chunking never copies the whole document,
    // unlike one giant request body that must fully buffer before sending.
    const blob = new Blob([content], { type: "text/plain; charset=utf-8" });
    const chunkSize = saveChunkBytes(boot);
    try {
      if (blob.size <= chunkSize) {
        setStatus("Saving…");
        const response = await fetch(boot.path, { method: "PUT", body: content, headers: { "Content-Type": "text/plain; charset=utf-8" } });
        requireAuthOk(response);
        if (!response.ok) throw new Error(`Save failed (${response.status})`);
      } else {
        setStatus("Saving… 0%");
        await saveChunked(blob, chunkSize);
      }
      saved.current = content; setDirty(false); setStatus("Saved");
      window.setTimeout(() => setStatus(""), 2500);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Save failed"); }
  };

  const navigateAway = (href: string): void => {
    if (pageLeaving) return;
    allowLeave.current = true;
    setPageLeaving(true);
    window.setTimeout(() => location.assign(href), window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 170);
  };

  const requestLeave = (href: string): void => {
    if (dirty) setConfirmLeave(href); else navigateAway(href);
  };

  const leave = (): void => { if (!confirmLeave) return; allowLeave.current = true; if (confirmLeave === "__logout__") { setAuthOverlay("signout"); window.setTimeout(() => logoutForm.current?.submit(), AUTH_REDIRECT_DELAY_MS); } else navigateAway(confirmLeave); };

  return <div className={`editor-app ${pageLeaving ? "page-leaving" : ""}`}>
    <header className="topbar editor-topbar"><div className="brand"><Logo/><span>X-wing</span><small>EDITOR</small></div><div className="editor-heading"><strong>{boot.filename}</strong><span>{status || (dirty ? "Unsaved changes" : boot.displayPath)}</span></div><div className="editor-actions"><a className="button" href={boot.path} download>Download</a><button className="button primary" disabled={!canEdit || !dirty} onClick={() => void save()}>Save</button>{boot.user.authenticated ? <div className="account-inline"><span>{boot.user.name}</span><form ref={logoutForm} id="logout-form" method="post" action="/_auth/logout" onSubmit={event => { event.preventDefault(); if (dirty) setConfirmLeave("__logout__"); else { setAuthOverlay("signout"); const form = event.currentTarget; window.setTimeout(() => form.submit(), AUTH_REDIRECT_DELAY_MS); } }}><button className="signout-button" type="submit">Sign out</button></form></div> : <span className="anonymous-label">anonymous</span>}</div></header>
    {(!boot.canWrite || boot.truncated) && <div className="editor-notices">
      {!boot.canWrite && <div className="readonly-notice">Read-only access. Saving changes is disabled.</div>}
      {boot.truncated && <div className="readonly-notice">Showing first {formatBytes(boot.previewBytes)} of {formatBytes(boot.totalSize)}. File too large to edit here — use Download for the full file.</div>}
    </div>}
    <div className="editor-body"><aside className="editor-rail"><button className="editor-back" onClick={() => requestLeave(boot.directory)} aria-label="Back to files">←</button><span>{boot.extension || "TXT"}</span></aside><div className="editor-canvas" ref={mount}/></div>
    {confirmLeave && <DiscardDialog onCancel={() => setConfirmLeave(null)} onDiscard={leave}/>} 
    {authOverlay && <div className="auth-overlay" role="status"><div className="auth-overlay-card"><span className="auth-pulse"><span/></span><div><h2>{authOverlay === "signout" ? "Signing out" : "Session expired"}</h2><p>{authOverlay === "signout" ? "Ending your session…" : "Redirecting to sign in…"}</p></div></div></div>}
  </div>;
}

function DiscardDialog({ onCancel, onDiscard }: { onCancel: () => void; onDiscard: () => void }): React.JSX.Element {
  const modalRef = useModalFocus<HTMLDivElement>(onCancel);
  return <div ref={modalRef} className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="discard-title" aria-describedby="discard-description"><h2 id="discard-title">Discard unsaved changes?</h2><p id="discard-description">This file has unsaved edits. Leave without saving?</p><div className="modal-actions"><button className="button" onClick={onCancel}>Keep editing</button><button data-autofocus className="button danger" onClick={onDiscard}>Discard changes</button></div></div></div>;
}

function loginUrl(): string { return `/_auth/login?redirect=${encodeURIComponent(location.pathname + location.search)}`; }

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let value = n;
  let unit = "B";
  for (const next of units) { value /= 1024; unit = next; if (value < 1024) break; }
  return `${value.toFixed(1)} ${unit}`;
}

function detectLanguage(cm: CodeMirrorApi, extension: string): unknown[] {
  const aliases: Record<string, string> = { py:"python",js:"javascript",jsx:"javascript",ts:"javascript",tsx:"javascript",html:"html",htm:"html",css:"css",json:"json",yaml:"yaml",yml:"yaml",md:"markdown",xml:"xml",svg:"xml",sql:"sql",sh:"shell",bash:"shell",zsh:"shell",toml:"toml",dockerfile:"dockerfile",nginx:"nginx" };
  const factory = cm.langs[aliases[extension] || extension];
  if (!factory) return [];
  try { return [factory()]; } catch { return []; }
}

const node = document.getElementById("xwing-editor-bootstrap");
const root = document.getElementById("xwing-editor-root");
if (node?.textContent && root) createRoot(root).render(<EditorApp boot={JSON.parse(node.textContent) as EditorBootstrap}/>);
