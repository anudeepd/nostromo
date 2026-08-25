import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import { useModalFocus } from "./keyboard";
import { nearestSurvivor, selectionRange } from "./selection";
import { nextSort, normalizeSortPreference, sortFiles } from "./sort";
import type { SortEntry, SortKey } from "./sort";
import { DIRECTORY_MEDIA_TYPE, encodePath, parseBootstrap } from "./types";
import type { Parallelism, XwingBootstrapV1, XwingFile } from "./types";
import { UploadManager } from "./upload-manager";
import { uploadItemLabel, uploadSummary, uploadSummaryKind } from "./upload-summary";

interface Toast {
  id: number;
  message: string;
  kind: "success" | "error" | "deleted" | "restored";
  duration: number;
  action?: { label: string; run: () => void };
}
type Dialog =
  | { kind: "mkdir"; value: string; error?: string | undefined }
  | { kind: "delete"; paths: string[]; pending: boolean; error?: string | undefined }
  | null;

type DropWaitState = "preparing" | "delayed" | null;

const uploadManager = new UploadManager();
const PARALLEL_VALUES: Parallelism[] = [1, 2, 4, 8];
const AUTH_REDIRECT_DELAY_MS = 1500;
const SORT_STORAGE_VERSION = "v2";
const DRAG_OVERLAY_STALE_MS = 1500;
const DROP_DELAYED_MS = 15000;
const DELETE_KEYS: Record<string, true> = { Delete: true, Backspace: true };

function sortStorageKey(user: string): string {
  return `xwing.sort.${SORT_STORAGE_VERSION}.${user}`;
}

function Logo(): React.JSX.Element {
  return <svg className="brand-mark" viewBox="0 0 200 200" aria-label="X-wing logo">
    <rect x="6" y="6" width="188" height="188" rx="36" />
    <g fill="none" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="71,78 23,48 15,100 23,152 71,122" /><polyline points="71,78 30,100 71,122" />
      <polygon points="129,78 177,48 185,100 177,152 129,122" /><polyline points="129,78 170,100 129,122" />
      <path d="m71 78 15 8m-15 36 15-8m43-36-15 8m15 36-15-8" />
      <circle cx="100" cy="100" r="20" /><circle cx="100" cy="100" r="13" />
    </g><circle className="brand-core" cx="100" cy="100" r="4.5" />
  </svg>;
}

function Icon({ name }: { name: string }): React.JSX.Element {
  const paths: Record<string, React.ReactNode> = {
    upload: <g transform="translate(0 .5)"><path d="M12 16V4m0 0L7 9m5-5 5 5"/><path d="M4 15v4h16v-4"/></g>,
    folderUpload: <g transform="translate(0 -1.5)"><path d="M3 7h7l2 2h9v11H3z"/><path d="M12 16v-5m0 0-2 2m2-2 2 2"/></g>,
    folderAdd: <g transform="translate(0 -1.5)"><path d="M3 7h7l2 2h9v11H3z"/><path d="M12 12v5m-2.5-2.5h5"/></g>,
    folder: <path d="M3 7h7l2 2h9v11H3z"/>,
    file: <><path d="M6 2h9l4 4v16H6z"/><path d="M15 2v5h5"/></>,
    download: <><path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M4 19h16"/></>,
    trash: <><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7"/><path d="M10 11v5m4-5v5"/></>,
    check: <path d="m5 12.5 4.25 4.25L19 7.5"/>, chevron: <path d="m7 10 5 5 5-5"/>,
    close: <path d="m6 6 12 12M18 6 6 18"/>, retry: <path d="M20 11a8 8 0 1 0-2 5.3M20 4v7h-7"/>,
  };
  return <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

function readBootstrap(): XwingBootstrapV1 {
  const node = document.getElementById("xwing-bootstrap");
  if (!node?.textContent) throw new Error("X-wing bootstrap data is missing");
  return parseBootstrap(JSON.parse(node.textContent));
}

function useOutsideClose(ref: React.RefObject<HTMLElement | null>, close: () => void): void {
  useEffect(() => {
    const handler = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [close, ref]);
}

function App({ initial }: { initial: XwingBootstrapV1 }): React.JSX.Element {
  const [directory, setDirectory] = useState(initial);
  const [directoryState, setDirectoryState] = useState<"ready" | "loading" | "error">("ready");
  const [directoryError, setDirectoryError] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [lastSelected, setLastSelected] = useState<string | null>(null);
  const [sort, setSort] = useState<SortEntry[]>(() => readSort(initial.user.name));
  const [parallelOpen, setParallelOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dragging, setDragging] = useState(false);
  const [dropWaitState, setDropWaitState] = useState<DropWaitState>(null);
  const [arrivingNames, setArrivingNames] = useState<Set<string>>(() => new Set());
  const [pageLeaving, setPageLeaving] = useState(false);
  const [authOverlay, setAuthOverlay] = useState<"signout" | "expired" | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const parallelRef = useRef<HTMLDivElement>(null);
  const accountRef = useRef<HTMLDivElement>(null);
  const requestId = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const dragDepth = useRef(0);
  const dragOverlayTimer = useRef<number | null>(null);
  const dropDelayTimer = useRef<number | null>(null);
  const completedUploads = useRef(new Set<string>());
  const pendingArrivalNames = useRef(new Set<string>());
  const autoRefreshTimer = useRef<number | null>(null);
  const arrivalTimer = useRef<number | null>(null);
  const currentDirectory = useRef(directory.path);
  currentDirectory.current = directory.path;
  const upload = useSyncExternalStore(uploadManager.subscribe, uploadManager.getSnapshot);

  useOutsideClose(parallelRef, () => setParallelOpen(false));
  useOutsideClose(accountRef, () => setAccountOpen(false));

  useEffect(() => {
    const handleGlobalKey = (event: KeyboardEvent): void => {
      if (event.defaultPrevented || document.querySelector("[aria-modal='true']")) return;
      if (event.key === "Escape" && accountOpen) {
        event.preventDefault();
        setAccountOpen(false);
        accountRef.current?.querySelector<HTMLElement>(".account-trigger")?.focus();
      } else if (event.key === "Escape" && parallelOpen) {
        event.preventDefault();
        setParallelOpen(false);
        parallelRef.current?.querySelector<HTMLElement>(".parallel-trigger")?.focus();
      } else if (event.key === "Escape" && selected.size) {
        event.preventDefault();
        setSelected(new Set());
        setLastSelected(null);
      } else if (DELETE_KEYS[event.key] && selected.size && directory.permissions.delete && !parallelOpen) {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.matches("input, textarea, select") || target?.isContentEditable) return;
        event.preventDefault();
        setDialog({ kind: "delete", paths: [...selected], pending: false });
      }
    };
    document.addEventListener("keydown", handleGlobalKey);
    return () => document.removeEventListener("keydown", handleGlobalKey);
  }, [accountOpen, directory.permissions.delete, parallelOpen, selected]);

  const addToast = (message: string, kind: Toast["kind"] = "success", action?: Toast["action"], duration = 5200): void => {
    const id = Date.now() + Math.random();
    const toast: Toast = action ? { id, message, kind, action, duration } : { id, message, kind, duration };
    setToasts(current => [...current.slice(-2), toast]);
  };

  const dismissToast = (id: number): void => setToasts(current => current.filter(item => item.id !== id));

  const openDocument = (href: string): void => {
    if (pageLeaving) return;
    setPageLeaving(true);
    window.setTimeout(() => location.assign(href), prefersReducedMotion() ? 0 : 170);
  };

  const navigate = async (path: string, historyMode: "push" | "replace" | "none" = "push"): Promise<void> => {
    const id = ++requestId.current;
    abort.current?.abort();
    const animate = historyMode === "push" && !prefersReducedMotion();
    if (animate) {
      setPageLeaving(true);
      await new Promise(resolve => window.setTimeout(resolve, 170));
      if (id !== requestId.current) return;
    } else setPageLeaving(false);
    const controller = new AbortController();
    abort.current = controller;
    setDirectoryState("loading");
    setDirectoryError("");
    try {
      const target = encodePath(path);
      const response = await authFetch(target, {
        headers: { Accept: DIRECTORY_MEDIA_TYPE }, signal: controller.signal,
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (!response.headers.get("content-type")?.includes(DIRECTORY_MEDIA_TYPE)) throw new Error("The server returned an unexpected response");
      const next = parseBootstrap(await response.json());
      if (id !== requestId.current) return;
      setDirectory(next);
      setSelected(new Set());
      setLastSelected(null);
      setDirectoryState("ready");
      document.title = `X-wing — ${next.path}`;
      const url = encodePath(next.path === "/" ? "/" : `${next.path}/`);
      if (historyMode === "push") history.pushState({ path: next.path }, "", url);
      if (historyMode === "replace") history.replaceState({ path: next.path }, "", url);
      if (animate) setPageLeaving(false);
      focusFileRow(null, true);
    } catch (error) {
      if (controller.signal.aborted) return;
      setDirectoryState("error");
      setDirectoryError(errorMessage(error));
      if (animate) setPageLeaving(false);
    }
  };

  useEffect(() => {
    history.replaceState({ path: initial.path }, "", location.href);
    const onPop = () => void navigate(location.pathname, "none");
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (uploadManager.hasActive()) event.preventDefault();
    };
    window.addEventListener("popstate", onPop);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => { window.removeEventListener("popstate", onPop); window.removeEventListener("beforeunload", onBeforeUnload); };
  }, []);

  useEffect(() => {
    const seconds = Number.parseInt(document.body.dataset.authIdleTimeout || "0", 10);
    if (!seconds) return;
    let deadline = Date.now() + seconds * 1000;
    let timer = window.setTimeout(expire, seconds * 1000);
    function expire(): void {
      if (Date.now() < deadline) { timer = window.setTimeout(expire, deadline - Date.now()); return; }
      setAuthOverlay("expired");
      window.setTimeout(() => location.assign(`/_auth/login?redirect=${encodeURIComponent(location.pathname + location.search)}`), AUTH_REDIRECT_DELAY_MS);
    }
    function activity(): void { deadline = Date.now() + seconds * 1000; window.clearTimeout(timer); timer = window.setTimeout(expire, seconds * 1000); }
    const events = ["pointerdown", "keydown", "touchstart", "wheel"] as const;
    events.forEach(name => window.addEventListener(name, activity, { passive: true }));
    return () => { window.clearTimeout(timer); events.forEach(name => window.removeEventListener(name, activity)); };
  }, []);

  const files = useMemo(() => sortFiles(directory.files, sort), [directory.files, sort]);

  const updateSort = (key: SortKey): void => {
    setSort(current => {
      const next = nextSort(current, key);
      localStorage.setItem(sortStorageKey(directory.user.name), JSON.stringify(next));
      return next;
    });
  };

  const toggleSelection = (file: XwingFile, index: number, gesture: { range: boolean; additive: boolean }): void => {
    setSelected(current => {
      if (!gesture.range && !gesture.additive) {
        return current.size === 1 && current.has(file.path) ? new Set() : new Set([file.path]);
      }
      const next = new Set(current);
      const shouldSelect = !current.has(file.path);
      if (gesture.range) {
        for (const path of selectionRange(files, lastSelected, index)) {
          if (shouldSelect) next.add(path);
          else next.delete(path);
        }
      } else if (shouldSelect) next.add(file.path);
      else next.delete(file.path);
      return next;
    });
    setLastSelected(file.path);
  };

  const refresh = (): Promise<void> => navigate(directory.path, "none");

  useEffect(() => {
    const newlyCompleted = upload.items.filter(item => item.status === "completed" && !completedUploads.current.has(item.id));
    for (const item of newlyCompleted) completedUploads.current.add(item.id);

    const relevant = newlyCompleted.filter(item => item.destination === directory.path);
    if (!relevant.length) return;

    for (const item of relevant) {
      const topLevelName = item.relativePath.split("/").find(Boolean);
      if (topLevelName) pendingArrivalNames.current.add(topLevelName);
    }
    if (autoRefreshTimer.current !== null) window.clearTimeout(autoRefreshTimer.current);
    const refreshPath = directory.path;
    autoRefreshTimer.current = window.setTimeout(() => {
      autoRefreshTimer.current = null;
      if (currentDirectory.current !== refreshPath) {
        pendingArrivalNames.current.clear();
        return;
      }
      const names = new Set(pendingArrivalNames.current);
      pendingArrivalNames.current.clear();
      setArrivingNames(names);
      void navigate(refreshPath, "none").finally(() => {
        if (arrivalTimer.current !== null) window.clearTimeout(arrivalTimer.current);
        arrivalTimer.current = window.setTimeout(
          () => setArrivingNames(new Set()),
          prefersReducedMotion() ? 0 : 700,
        );
      });
    }, prefersReducedMotion() ? 0 : 140);
  }, [directory.path, upload.items]);

  useEffect(() => () => {
    if (autoRefreshTimer.current !== null) window.clearTimeout(autoRefreshTimer.current);
    if (arrivalTimer.current !== null) window.clearTimeout(arrivalTimer.current);
  }, []);

  const createFolder = async (): Promise<void> => {
    if (!dialog || dialog.kind !== "mkdir") return;
    const value = dialog.value.trim();
    if (!value || value.includes("/")) { setDialog({ ...dialog, error: "Enter one valid folder name." }); return; }
    const target = `${directory.path === "/" ? "" : directory.path}/${encodeURIComponent(value)}/`;
    const response = await authFetch(target, { method: "MKCOL" });
    if (!response.ok) { setDialog({ ...dialog, error: await responseError(response) }); return; }
    setDialog(null); addToast(`Created ${value}`); await refresh();
  };

  const deletePaths = async (): Promise<void> => {
    if (!dialog || dialog.kind !== "delete") return;
    const focusAfterDelete = nearestSurvivor(files, dialog.paths);
    setDialog({ ...dialog, pending: true, error: undefined });
    const response = dialog.paths.length === 1
      ? await authFetch(dialog.paths[0]!, { method: "DELETE" })
      : await authFetch("/_bulk/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: dialog.paths }) });
    if (!response.ok) { setDialog({ ...dialog, pending: false, error: await responseError(response) }); return; }
    const data = await response.json().catch(() => ({})) as { transaction_id?: string };
    setDialog(null); setSelected(new Set());
    addToast(`${dialog.paths.length} item${dialog.paths.length === 1 ? "" : "s"} deleted`, "deleted", data.transaction_id ? {
      label: "Undo", run: () => void restore(data.transaction_id!),
    } : undefined, 15000);
    await refresh();
    focusFileRow(focusAfterDelete);
  };

  const restore = async (transaction: string): Promise<void> => {
    const response = await authFetch(`/api/restore/${transaction}`, { method: "POST" });
    if (!response.ok) addToast(await responseError(response), "error");
    else {
      const data = await response.json().catch(() => ({})) as { restored?: number };
      const count = data.restored ?? 0;
      addToast(count ? `${count} item${count === 1 ? "" : "s"} restored` : "Deleted items restored", "restored", undefined, 15000);
      await refresh();
    }
  };

  const downloadSelected = async (): Promise<void> => {
    const response = await authFetch("/_bulk/zip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [...selected], base: directory.path }) });
    if (!response.ok) { addToast(await responseError(response), "error"); return; }
    downloadBlob(await response.blob(), contentDispositionFilename(response.headers.get("content-disposition")) || "xwing-selection.zip");
  };

  const clearDropFeedback = useCallback((): void => {
    if (dragOverlayTimer.current !== null) {
      window.clearTimeout(dragOverlayTimer.current);
      dragOverlayTimer.current = null;
    }
    if (dropDelayTimer.current !== null) {
      window.clearTimeout(dropDelayTimer.current);
      dropDelayTimer.current = null;
    }
    dragDepth.current = 0;
    setDragging(false);
    setDropWaitState(null);
  }, []);

  const refreshDropFeedback = useCallback((): void => {
    setDragging(true);
    setDropWaitState(null);
    if (dragOverlayTimer.current !== null) window.clearTimeout(dragOverlayTimer.current);
    if (dropDelayTimer.current !== null) {
      window.clearTimeout(dropDelayTimer.current);
      dropDelayTimer.current = null;
    }
    dragOverlayTimer.current = window.setTimeout(() => {
      dragOverlayTimer.current = null;
      setDragging(false);
      setDropWaitState("preparing");
      dropDelayTimer.current = window.setTimeout(() => {
        dropDelayTimer.current = null;
        setDropWaitState("delayed");
      }, DROP_DELAYED_MS);
    }, DRAG_OVERLAY_STALE_MS);
  }, []);

  useEffect(() => {
    const clearWhenHidden = (): void => {
      if (document.hidden) clearDropFeedback();
    };
    window.addEventListener("drop", clearDropFeedback);
    window.addEventListener("dragend", clearDropFeedback);
    window.addEventListener("blur", clearDropFeedback);
    document.addEventListener("visibilitychange", clearWhenHidden);
    return () => {
      window.removeEventListener("drop", clearDropFeedback);
      window.removeEventListener("dragend", clearDropFeedback);
      window.removeEventListener("blur", clearDropFeedback);
      document.removeEventListener("visibilitychange", clearWhenHidden);
      if (dragOverlayTimer.current !== null) window.clearTimeout(dragOverlayTimer.current);
      if (dropDelayTimer.current !== null) window.clearTimeout(dropDelayTimer.current);
    };
  }, [clearDropFeedback]);

  const queueFiles = (list: FileList | File[]): void => {
    if (!directory.permissions.write || !list.length) return;
    uploadManager.add(Array.from(list), directory.path, directory.upload.chunkSize);
  };

  return <div className={`xw-app ${pageLeaving ? "page-leaving" : ""}`}
    onDragEnter={event => { event.preventDefault(); if (!directory.permissions.write) return; dragDepth.current += 1; refreshDropFeedback(); }}
    onDragOver={event => { if (!directory.permissions.write) return; event.preventDefault(); refreshDropFeedback(); }}
    onDragLeave={event => { event.preventDefault(); dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) clearDropFeedback(); }}
    onDrop={event => { event.preventDefault(); clearDropFeedback(); queueFiles(event.dataTransfer.files); }}>
    <a className="skip-link" href="#file-list">Skip to files</a>
    <header className="topbar">
      <div className="brand"><Logo/><span>X-wing</span><small className="brand-context">FILES</small></div>
      {directory.user.authenticated ? <div className="account-inline">{directory.admin ? <div className="account" ref={accountRef}>
        <button className="account-trigger" type="button" aria-haspopup="menu" aria-expanded={accountOpen} onClick={() => setAccountOpen(value => !value)}>
          <span>{directory.user.name}</span><Icon name="chevron"/>
        </button>
        {accountOpen && <div className="popover account-menu" role="menu" aria-label="Workspace navigation">
          <a className="menu-item active" href="/" role="menuitem" aria-current="page"><span className="workspace-nav-dot" aria-hidden="true"/>Files</a>
          <a className="menu-item" href="/admin" role="menuitem"><span className="workspace-nav-dot" aria-hidden="true"/>Admin panel</a>
        </div>}
      </div> : <span>{directory.user.name}</span>}<form id="logout-form" method="post" action="/_auth/logout" onSubmit={event => { event.preventDefault(); setAuthOverlay("signout"); const form = event.currentTarget; window.setTimeout(() => form.submit(), AUTH_REDIRECT_DELAY_MS); }}><button className="signout-button" type="submit">Sign out</button></form></div> : <span className="anonymous-label">anonymous</span>}
    </header>

    <main className={`workspace ${dragging ? "dragging" : ""} ${directoryState === "loading" ? "directory-loading" : ""}`}>
      <section className="location" aria-label="Current location">
        <nav className="crumbs" aria-label="Breadcrumb">{directory.breadcrumbs.map((crumb, index) => <React.Fragment key={crumb.path}>
          {index > 0 && <span className="slash">/</span>}
          {index === directory.breadcrumbs.length - 1 ? <span className="crumb current">{crumb.name === "Home" ? "workspace" : crumb.name}</span> : <a className="crumb" href={crumb.path} onClick={event => { event.preventDefault(); void navigate(crumb.path); }}>{crumb.name === "Home" ? "workspace" : crumb.name}</a>}
        </React.Fragment>)}</nav>
        <div className="location-meta"><span>{directory.files.length} items</span></div>
      </section>

      <div className="workspace-controls">
      {!directory.permissions.write && <div className="readonly-notice" role="status">Read-only access. Uploads and folder creation are disabled.</div>}
      <section className="actionbar" aria-label="File actions">
        <div className="toolbar-group">
          <button className="button primary" disabled={!directory.permissions.write} onClick={() => fileInput.current?.click()}><Icon name="upload"/><span className="label">Upload files</span></button>
          <button className="button hide-tablet" aria-label="Upload folder" disabled={!directory.permissions.write} onClick={() => folderInput.current?.click()}><Icon name="folderUpload"/><span className="label">Upload folder</span></button>
          <button className="button" aria-label="New folder" disabled={!directory.permissions.write} onClick={() => setDialog({ kind: "mkdir", value: "" })}><Icon name="folderAdd"/><span className="label">New folder</span></button>
          <input ref={fileInput} type="file" multiple hidden onChange={event => { if (event.target.files) { clearDropFeedback(); queueFiles(event.target.files); } event.currentTarget.value = ""; }}/>
          <input ref={folderInput} type="file" multiple hidden {...({ webkitdirectory: "" } as React.InputHTMLAttributes<HTMLInputElement>)} onChange={event => { if (event.target.files) { clearDropFeedback(); queueFiles(event.target.files); } event.currentTarget.value = ""; }}/>
        </div>
        <div className={`toolbar-group selection-actions ${selected.size ? "visible" : ""}`} aria-hidden={!selected.size}>
          <span className="selection-pill"><i/>{selected.size} selected</span>
          <button className="button" aria-label="Download selected as zip" disabled={!selected.size} onClick={() => void downloadSelected()}><Icon name="download"/><span className="label">Download zip</span></button>
          <button className="button danger" aria-label="Delete selected" disabled={!selected.size || !directory.permissions.delete} onClick={() => setDialog({ kind: "delete", paths: [...selected], pending: false })}><Icon name="trash"/><span className="label">Delete</span></button>
          <button className="button ghost" disabled={!selected.size} onClick={() => { const focusPath = lastSelected ?? selected.values().next().value ?? null; setSelected(new Set()); setLastSelected(null); focusFileRow(focusPath); }}>Clear</button>
        </div>
        <div className="toolbar-group toolbar-end">
          <div className="parallel-wrap" ref={parallelRef}>
            <button className="parallel-trigger" aria-label={`Parallel uploads: ${upload.parallel}`} aria-haspopup="dialog" aria-expanded={parallelOpen} onClick={() => setParallelOpen(value => !value)}>
              <span className="parallel-copy"><span>Parallel</span><strong>{upload.parallel}</strong></span><Icon name="chevron"/>
            </button>
            {parallelOpen && <div className="popover parallel-menu" role="dialog" aria-label="Concurrent uploads">
              <div className="menu-title">Concurrent uploads</div>
              <div role="radiogroup">{PARALLEL_VALUES.map(value => <label className={`parallel-option ${upload.parallel === value ? "selected" : ""}`} key={value}>
                <input type="radio" name="parallel" value={value} checked={upload.parallel === value} onChange={() => { uploadManager.setParallel(value); setParallelOpen(false); }}/>
                <span><strong>{value}</strong><small>at a time</small></span>{upload.parallel === value && <Icon name="check"/>}
              </label>)}</div>
            </div>}
          </div>
        </div>
      </section>
      </div>

      <section className="file-surface" aria-label="Files and folders" aria-busy={directoryState === "loading"} onKeyDown={event => {
        if ((event.target as Element).closest(".file-row")) return;
        if (DELETE_KEYS[event.key] && directory.permissions.delete && selected.size) { event.preventDefault(); setDialog({ kind: "delete", paths: [...selected], pending: false }); }
        else if (event.key === "Escape" && selected.size) { event.preventDefault(); setSelected(new Set()); setLastSelected(null); }
      }}>
        <div className="table-head"><SelectionCheckbox label={selected.size === files.length ? "Deselect all" : "Select all"} checked={files.length > 0 && selected.size === files.length} indeterminate={selected.size > 0 && selected.size < files.length} onToggle={() => { setSelected(selected.size === files.length ? new Set() : new Set(files.map(file => file.path))); setLastSelected(null); }}/><span/>{(["name", "size", "modified"] as SortKey[]).map(key => { const index = sort.findIndex(entry => entry.key === key); const entry = sort[index]; const label = key === "modified" ? "Modified" : key[0]!.toUpperCase() + key.slice(1); return <button key={key} className={`sort ${key === "modified" ? "date" : ""} ${entry ? "active" : ""}`} aria-label={`${label}, ${entry ? `${entry.direction === "asc" ? "ascending" : "descending"}, priority ${index + 1}` : "not sorted"}`} onClick={() => updateSort(key)}>{label} {entry && <span>{entry.direction === "asc" ? "▲" : "▼"}{sort.length > 1 ? index + 1 : ""}</span>}</button>; })}<span/></div>
        <div id="file-list" className="file-list" tabIndex={-1}>
          {directoryState === "error" && <div className="state-panel"><strong>Couldn’t open this folder</strong><span>{directoryError}</span><button className="button" onClick={() => void refresh()}>Retry</button></div>}
          {!files.length && directoryState !== "error" && <div className="state-panel empty"><span className="empty-icon"><Icon name="folder"/></span><strong>This folder is empty</strong><span>{directory.permissions.write ? "Upload files or create a folder to get started." : "You have read-only access here."}</span></div>}
          {files.map((file, index) => <FileRow key={file.path} file={file} selected={selected.has(file.path)} loading={directoryState === "loading"} arriving={arrivingNames.has(file.name)} onSelect={(gesture) => toggleSelection(file, index, gesture)} onOpen={() => file.kind === "directory" ? void navigate(file.path) : openDocument(`${file.path}${file.editable ? "?edit" : ""}`)} onDelete={() => setDialog({ kind: "delete", paths: [file.path], pending: false })} onDeleteKey={() => { if (directory.permissions.delete) setDialog({ kind: "delete", paths: selected.size ? [...selected] : [file.path], pending: false }); }} onClear={() => { setSelected(new Set()); setLastSelected(null); }}/>) }
        </div>
        <div className="statusbar"><span className="drop-hint">Drop files anywhere to upload</span></div>
        {directoryState === "loading" && <div className="loading-line"/>}
        {dropWaitState && <div className={`drop-wait ${dropWaitState}`}>
          {dropWaitState === "preparing"
            ? <span className="drop-wait-spinner" aria-hidden="true"/>
            : <span className="drop-wait-icon" aria-hidden="true"><Icon name="upload"/></span>}
          <span role="status" aria-live="polite">{dropWaitState === "preparing" ? "Preparing upload…" : "Upload hasn't started yet."}</span>
          {dropWaitState === "delayed" && <button className="button" type="button" onClick={() => fileInput.current?.click()}>Choose files</button>}
          <button className="icon-button" type="button" aria-label="Dismiss upload status" onClick={clearDropFeedback}><Icon name="close"/></button>
        </div>}
      </section>
      {dragging && <div className="drop-target" role="status" aria-live="polite"><span className="drop-target-icon"><Icon name="upload"/></span><strong>Drop files here</strong><span>Upload to {directory.path}</span></div>}
      <UploadDock snapshot={upload}/>
      <div className="toast-stack" aria-live="polite">{toasts.map(toast => <ToastView key={toast.id} toast={toast} onDismiss={() => dismissToast(toast.id)}/>)}</div>
    </main>
    {dialog && <DialogView dialog={dialog} setDialog={setDialog} onMkdir={() => void createFolder()} onDelete={() => void deletePaths()}/>} 
    {authOverlay && <div className="auth-overlay" role="status" aria-live="polite"><div className="auth-overlay-card"><span className="auth-pulse"><span/></span><div><h2>{authOverlay === "signout" ? "Signing out" : "Session expired"}</h2><p>{authOverlay === "signout" ? "Ending your session…" : "Your session has ended. Redirecting to sign in…"}</p></div></div></div>}
  </div>;
}

function ToastView({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }): React.JSX.Element {
  useEffect(() => {
    const timer = window.setTimeout(onDismiss, toast.duration);
    return () => window.clearTimeout(timer);
  }, [toast.id, toast.duration]);
  const icon = toast.kind === "deleted" || toast.kind === "error" ? "trash" : "check";
  return <div className={`toast ${toast.kind}`} role={toast.kind === "error" ? "alert" : "status"}>
    <span className="toast-icon"><Icon name={icon}/></span>
    <span className="toast-message">{toast.message}</span>
    {toast.action && <button className="toast-action" onClick={() => { onDismiss(); toast.action?.run(); }}>{toast.action.label}</button>}
    <span className="toast-timer" aria-hidden="true" style={{ animationDuration: `${toast.duration}ms` }}/>
  </div>;
}

function FileRow({ file, selected, loading, arriving, onSelect, onOpen, onDelete, onDeleteKey, onClear }: { file: XwingFile; selected: boolean; loading: boolean; arriving: boolean; onSelect: (gesture: { range: boolean; additive: boolean }) => void; onOpen: () => void; onDelete: () => void; onDeleteKey: () => void; onClear: () => void }): React.JSX.Element {
  const moveFocus = (row: HTMLElement, direction: "next" | "previous" | "first" | "last"): void => {
    const rows = [...(row.parentElement?.querySelectorAll<HTMLElement>(".file-row") ?? [])];
    const current = rows.indexOf(row);
    const target = direction === "first" ? rows[0] : direction === "last" ? rows[rows.length - 1] : rows[current + (direction === "next" ? 1 : -1)];
    target?.focus();
  };
  return <div className={`file-row ${selected ? "selected" : ""} ${loading ? "muted" : ""} ${arriving ? "arriving" : ""}`} role="row" aria-label={`${file.name}, ${file.kind}`} aria-selected={selected} data-path={file.path} tabIndex={0}
    onMouseDown={event => { if (event.shiftKey && !(event.target as Element).closest(".row-actions")) event.preventDefault(); }}
    onDragStart={event => event.preventDefault()}
    onClick={event => { if ((event.target as Element).closest(".row-actions")) return; window.getSelection()?.removeAllRanges(); onSelect({ range: event.shiftKey, additive: event.metaKey || event.ctrlKey }); event.currentTarget.focus(); }}
    onDoubleClick={event => { if (!(event.target as Element).closest(".row-actions")) onOpen(); }}
    onKeyDown={event => {
      const rowOwnsKey = event.target === event.currentTarget;
      if (!rowOwnsKey && event.key !== "Delete" && event.key !== "Escape") return;
      if (event.key === " ") { event.preventDefault(); onSelect({ range: event.shiftKey, additive: !event.shiftKey }); }
      else if (event.key === "Enter") { event.preventDefault(); onOpen(); }
      else if (event.key === "Delete") { event.preventDefault(); onDeleteKey(); }
      else if (event.key === "Escape") { event.preventDefault(); onClear(); }
      else if (event.key === "ArrowDown") { event.preventDefault(); moveFocus(event.currentTarget, "next"); }
      else if (event.key === "ArrowUp") { event.preventDefault(); moveFocus(event.currentTarget, "previous"); }
      else if (event.key === "Home") { event.preventDefault(); moveFocus(event.currentTarget, "first"); }
      else if (event.key === "End") { event.preventDefault(); moveFocus(event.currentTarget, "last"); }
    }}>
    <SelectionCheckbox rowControl label={`${selected ? "Deselect" : "Select"} ${file.name}`} checked={selected} onToggle={event => onSelect({ range: event.shiftKey, additive: event.metaKey || event.ctrlKey || !event.shiftKey })}/>
    <span className={`file-icon ${file.kind}`}><Icon name={file.kind === "directory" ? "folder" : "file"}/></span>
    <span className={`filename ${file.kind}`} title={file.name}>{file.name}{file.kind === "directory" ? "/" : ""}</span>
    <span className="cell">{file.size === null ? "—" : formatBytes(file.size)}</span>
    <span className="cell date">{file.modified ? formatDate(file.modified) : "—"}</span>
    <span className="row-actions"><a className="icon-button" href={file.kind === "directory" ? `${file.path}?zip` : file.path} download aria-label={`Download ${file.name}`}><Icon name="download"/></a><button className="icon-button danger-icon" aria-label={`Delete ${file.name}`} onClick={onDelete}><Icon name="trash"/></button></span>
  </div>;
}

function SelectionCheckbox({ label, checked, indeterminate = false, rowControl = false, onToggle }: { label: string; checked: boolean; indeterminate?: boolean; rowControl?: boolean; onToggle: (event: React.MouseEvent<HTMLInputElement>) => void }): React.JSX.Element {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate; }, [indeterminate]);
  return <input ref={ref} className="selection-checkbox" type="checkbox" aria-label={label} checked={checked} onChange={() => undefined} onClick={event => { event.stopPropagation(); onToggle(event); if (rowControl && event.detail > 0) event.currentTarget.closest<HTMLElement>(".file-row")?.focus(); }}/>;
}

function focusFileRow(path: string | null, fallbackToFirst = false): void {
  window.requestAnimationFrame(() => {
    const rows = [...document.querySelectorAll<HTMLElement>(".file-row")];
    (rows.find(row => row.dataset.path === path) ?? (fallbackToFirst ? rows[0] : null) ?? document.getElementById("file-list"))?.focus();
  });
}

function UploadDock({ snapshot }: { snapshot: ReturnType<UploadManager["getSnapshot"]> }): React.JSX.Element | null {
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    setClosing(false);
    const hasCompleted = snapshot.items.some(item => item.status === "completed");
    const hasActive = snapshot.items.some(item => ["queued", "preparing", "uploading", "retrying"].includes(item.status));
    if (!hasCompleted || hasActive) return;

    let dismissTimer: number | null = null;
    const waitTimer = window.setTimeout(() => {
      const allSuccessful = snapshot.items.every(item => item.status === "completed");
      if (allSuccessful) setClosing(true);
      dismissTimer = window.setTimeout(
        () => uploadManager.dismissSuccessful(),
        allSuccessful && !prefersReducedMotion() ? 180 : 0,
      );
    }, 4000);
    return () => {
      window.clearTimeout(waitTimer);
      if (dismissTimer !== null) window.clearTimeout(dismissTimer);
    };
  }, [snapshot.items]);

  if (!snapshot.items.length) return null;
  const dismissible = snapshot.items.some(item => item.status === "completed" || item.status === "cancelled");
  return <aside className={`upload-dock ${closing ? "closing" : ""}`} aria-label="Uploads"><div className="upload-header"><div><strong>Uploads</strong><span className={`upload-summary ${uploadSummaryKind(snapshot)}`}>{uploadSummary(snapshot)}</span></div><button className="icon-button" aria-label="Clear finished uploads" disabled={!dismissible} onClick={() => uploadManager.dismissCompleted()}><Icon name="close"/></button></div>
    <div className="upload-items">{snapshot.items.map(item => <div className={`upload-item ${item.status}`} key={item.id}><div className="upload-line"><span title={item.relativePath}>{item.relativePath}</span><strong>{item.status === "completed" ? "Done" : `${Math.round(item.size ? item.uploaded / item.size * 100 : 0)}%`}</strong></div><div className="progress-track"><span style={{ width: `${item.size ? item.uploaded / item.size * 100 : 0}%` }}/></div><div className="upload-meta"><span>{uploadItemLabel(item)}{item.status === "uploading" && item.speed > 0 ? ` · ${formatBytes(item.speed)}/s` : ""}</span><span>{item.status === "failed" || item.status === "cancelled" ? <button onClick={() => uploadManager.retry(item.id)}><Icon name="retry"/> Retry</button> : item.status !== "completed" ? <button onClick={() => uploadManager.cancel(item.id)}>Cancel</button> : null}</span></div></div>)}</div>
  </aside>;
}

function DialogView({ dialog, setDialog, onMkdir, onDelete }: { dialog: Exclude<Dialog, null>; setDialog: (value: Dialog) => void; onMkdir: () => void; onDelete: () => void }): React.JSX.Element {
  const mkdir = dialog.kind === "mkdir";
  const pending = "pending" in dialog && dialog.pending;
  const [closing, setClosing] = useState(false);
  const close = (): void => {
    if (pending || closing) return;
    setClosing(true);
    window.setTimeout(() => setDialog(null), prefersReducedMotion() ? 0 : 160);
  };
  const modalRef = useModalFocus<HTMLDivElement>(close, !pending && !closing);
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!pending && modalRef.current && !modalRef.current.contains(document.activeElement)) confirmRef.current?.focus();
  }, [pending, modalRef]);
  return <div ref={modalRef} className={`modal-backdrop ${closing ? "closing" : ""}`} onMouseDown={event => { if (event.target === event.currentTarget) close(); }}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="dialog-title" aria-describedby="dialog-description" onSubmit={event => { event.preventDefault(); if (mkdir) onMkdir(); else onDelete(); }}>
    <h2 id="dialog-title">{mkdir ? "New folder" : `Delete ${dialog.paths.length} item${dialog.paths.length === 1 ? "" : "s"}?`}</h2>
    <p id="dialog-description">{mkdir ? "Create a folder in the current directory." : "The items will move to X-wing’s recoverable trash."}</p>
    {mkdir && <label>Folder name<input data-autofocus value={dialog.value} onChange={event => setDialog({ ...dialog, value: event.target.value, error: undefined })}/></label>}
    {dialog.error && <div className="dialog-error" role="alert">{dialog.error}</div>}
    <div className="modal-actions"><button type="button" className="button" disabled={pending || closing} onClick={close}>Cancel</button><button ref={confirmRef} data-autofocus={!mkdir ? "true" : undefined} className={`button ${mkdir ? "primary" : "danger"}`} disabled={pending || closing}>{pending ? "Deleting…" : mkdir ? "Create folder" : "Delete"}</button></div>
  </form></div>;
}

async function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  if (response.status === 401 || new URL(response.url || location.href, location.href).pathname === "/_auth/login") {
    location.assign(`/_auth/login?redirect=${encodeURIComponent(location.pathname + location.search)}`);
    throw new Error("authentication required");
  }
  return response;
}

function readSort(user: string): SortEntry[] {
  try {
    return normalizeSortPreference(JSON.parse(localStorage.getItem(sortStorageKey(user)) || "null") as unknown);
  } catch { return normalizeSortPreference(null); }
}

function formatBytes(bytes: number): string { const units = ["B", "KB", "MB", "GB", "TB"]; let value = bytes; let unit = 0; while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; } return `${unit ? value.toFixed(1) : value} ${units[unit]}`; }
function formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "Something went wrong"; }
async function responseError(response: Response): Promise<string> { try { const body = await response.json() as { detail?: string }; return body.detail || `Request failed (${response.status})`; } catch { return `Request failed (${response.status})`; } }
function downloadBlob(blob: Blob, filename: string): void { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function contentDispositionFilename(header: string | null): string | null { const match = header?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i); return match?.[1] ? decodeURIComponent(match[1]) : null; }
function prefersReducedMotion(): boolean { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }

try {
  const root = document.getElementById("xwing-root");
  if (!root) throw new Error("X-wing root is missing");
  createRoot(root).render(<App initial={readBootstrap()}/>);
} catch (error) {
  const root = document.getElementById("xwing-root") || document.body;
  root.innerHTML = `<div class="boot-error"><strong>X-wing couldn’t start</strong><span>${escapeHtml(errorMessage(error))}</span><button onclick="location.reload()">Reload</button></div>`;
}

function escapeHtml(value: string): string { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }
