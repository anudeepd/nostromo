"use strict";

import { createDialogController, wireFileTableSelection } from "./app-core.js";
import { createAuthSession, isLoginResponseUrl } from "./shared.js";
import collectEntries, { appendPath, ensureDir as ensureDirWithFetch } from "./upload-entries.js";

const DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024;  // 8 MB
const TARGET_UPLOAD_CHUNKS = 128;
const MAX_BROWSER_CHUNK_SIZE = 64 * 1024 * 1024;  // 64 MB
const UPLOAD_PROGRESS_CAP = 95;
const UPLOAD_STALL_MS = 1500;
const UPLOAD_RETRY_DELAYS_MS = [750, 1500, 3000];
const RETRYABLE_UPLOAD_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);
const CURRENT_PATH = document.body.dataset.currentPath || "/";
const CURRENT_USER = document.body.dataset.user || "anonymous";
const CAN_WRITE = document.body.dataset.canWrite === "true";
const CAN_DELETE = document.body.dataset.canDelete === "true";
const SERVER_MAX_CHUNK_BYTES = parseInt(document.body.dataset.maxChunkBytes, 10) || DEFAULT_CHUNK_SIZE;
const SORT_STORAGE_KEY = `xwing.sort.${CURRENT_USER}`;
const AUTH_REDIRECT_DELAY_MS = 1500;
const AUTH_IDLE_GRACE_MS = 1000;
const AUTH_IDLE_TIMEOUT_SECONDS = parseInt(document.body.dataset.authIdleTimeout, 10) || 0;
const AUTH_ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart", "wheel"];
const auth = createAuthSession({
  redirectDelayMs: AUTH_REDIRECT_DELAY_MS,
  idleTimeoutSeconds: AUTH_IDLE_TIMEOUT_SECONDS,
  idleGraceMs: AUTH_IDLE_GRACE_MS,
  activityEvents: AUTH_ACTIVITY_EVENTS,
});
const authFetch = auth.authFetch;
const dialogs = createDialogController();

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function httpStatusError(status, message = `HTTP ${status}`) {
  const error = new Error(message);
  error.status = status;
  return error;
}

function isRetryableUploadError(error) {
  if (auth.isRedirecting()) return false;
  if (RETRYABLE_UPLOAD_STATUSES.has(error?.status)) return true;
  return error?.message === "network error" || error?.message === "timeout";
}

async function withUploadRetries(action, { label, ui } = {}) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await action();
    } catch (error) {
      if (!isRetryableUploadError(error) || attempt >= UPLOAD_RETRY_DELAYS_MS.length) {
        throw error;
      }
      const delay = UPLOAD_RETRY_DELAYS_MS[attempt];
      ui?.setStatus(`${label} failed (${error.message}); retrying ${attempt + 1}/${UPLOAD_RETRY_DELAYS_MS.length}...`);
      await sleep(delay);
    }
  }
}

async function warnReadOnly(action) {
  await dialogs.alert("Read-only access", `${action} is disabled for your user.`);
}

function getConcurrency() {
  return parseInt(document.getElementById("concurrency-select").value, 10) || 4;
}

function chunkSizeForFile(fileSize) {
  if (fileSize <= 0) return DEFAULT_CHUNK_SIZE;
  const targetSize = Math.ceil(fileSize / TARGET_UPLOAD_CHUNKS);
  const chunkSize = Math.max(DEFAULT_CHUNK_SIZE, targetSize);
  return Math.max(1, Math.min(chunkSize, MAX_BROWSER_CHUNK_SIZE, SERVER_MAX_CHUNK_BYTES));
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ── Zip download spinner overlay ───────────────────────────────────────────────
function createZipOverlay() {
  const overlay = document.createElement("div");
  overlay.id = "zip-overlay";
  overlay.className = "zip-overlay";
  overlay.setAttribute("role", "status");
  overlay.setAttribute("aria-live", "polite");
  overlay.hidden = true;

  const card = document.createElement("div");
  card.className = "zip-overlay-card";

  const spinner = document.createElement("span");
  spinner.className = "zip-spinner";
  spinner.setAttribute("aria-hidden", "true");

  const text = document.createElement("span");
  text.id = "zip-overlay-text";
  text.className = "zip-overlay-text";

  card.append(spinner, text);
  overlay.appendChild(card);
  document.body.appendChild(overlay);

  let hideTimer = null;
  function setBusy(count) {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
    if (count > 0) {
      text.textContent = `Zipping ${count} file${count === 1 ? "" : "s"}…`;
      overlay.classList.remove("hiding");
      overlay.hidden = false;
    } else {
      overlay.classList.add("hiding");
      hideTimer = setTimeout(() => {
        overlay.hidden = true;
        overlay.classList.remove("hiding");
        hideTimer = null;
      }, 150);
    }
  }

  return { setBusy };
}

const zipOverlay = createZipOverlay();

function filenameFromContentDisposition(header) {
  if (!header) return null;
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const plainMatch = header.match(/filename="?([^";]+)"?/i);
  return plainMatch ? plainMatch[1] : null;
}

function stagingFolderName(name) {
  const nonce = Math.random().toString(36).slice(2);
  return `.xwing-upload-${Date.now()}-${nonce}-${name}`;
}

function nextPaint() {
  return new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  });
}

// ── Date formatting ────────────────────────────────────────────────────────────
auth.wireLogoutForm();
auth.wireAuthIdleTimer();

document.querySelectorAll("[data-mtime]").forEach(td => {
  const ts = parseFloat(td.dataset.mtime);
  if (!isNaN(ts)) {
    td.textContent = new Date(ts * 1000).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }
});

// ── Selection + bulk actions ─────────────────────────────────────────────────
const selectAll = document.getElementById("select-all");
const fileTable = document.querySelector(".file-table");
const tableWrap = document.querySelector(".table-wrap");
const clearSelectionBtn = document.getElementById("clear-selection-btn");
const zipSelectedBtn = document.getElementById("zip-selected-btn");
const deleteSelectedBtn = document.getElementById("delete-selected-btn");
const selectionCount = document.getElementById("selection-count");
const filesRegion = document.getElementById("files-region");
const itemCount = document.querySelector(".item-count");
let tableSelection = null;

function selectableEntryCount() {
  return document.querySelectorAll(".selectable-entry").length;
}

function updateItemCount() {
  if (!itemCount) return;
  const count = selectableEntryCount();
  itemCount.textContent = `${count} item${count === 1 ? "" : "s"}`;
}

function renderEmptyFolderState() {
  const tbody = document.querySelector(".file-table tbody");
  if (!tbody || selectableEntryCount() > 0 || tbody.querySelector(".empty-state")) return;

  const row = document.createElement("tr");
  row.className = "empty-row";
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.className = "empty";

  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.setAttribute("role", "status");
  empty.setAttribute("aria-label", "This folder is empty");

  const icon = document.createElement("div");
  icon.className = "empty-state-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7h18v14H3z"/><path d="M3 7l3-4h6l3 4"/><path d="M9 14h6"/></svg>';

  const title = document.createElement("h2");
  title.textContent = "This folder is empty";
  const copy = document.createElement("p");
  copy.textContent = CAN_WRITE
    ? "Upload files or create a folder to get started."
    : "You have read-only access here.";

  empty.append(icon, title, copy);
  if (CAN_WRITE) {
    const actions = document.createElement("div");
    actions.className = "empty-state-actions";
    const upload = document.createElement("button");
    upload.className = "btn btn-primary";
    upload.type = "button";
    upload.dataset.emptyAction = "upload";
    upload.textContent = "Upload files";
    const mkdir = document.createElement("button");
    mkdir.className = "btn";
    mkdir.type = "button";
    mkdir.dataset.emptyAction = "mkdir";
    mkdir.textContent = "New folder";
    actions.append(upload, mkdir);
    empty.appendChild(actions);
  }

  cell.appendChild(empty);
  row.appendChild(cell);
  tbody.appendChild(row);
}

// ── Sorting ──────────────────────────────────────────────────────────────────
const SORT_KEYS = ["name", "size", "mtime"];
const DEFAULT_SORT = [{ key: "name", dir: "asc" }];
const sortHeaders = [...document.querySelectorAll(".sort-header")];
const resetSortBtn = document.getElementById("reset-sort-btn");
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function isValidSortEntry(entry) {
  return entry
    && SORT_KEYS.includes(entry.key)
    && ["asc", "desc"].includes(entry.dir);
}

function readSortPreference() {
  try {
    const pref = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY) || "null");
    if (Array.isArray(pref)) {
      const entries = pref.filter(isValidSortEntry);
      if (entries.length) return entries;
    }
    if (isValidSortEntry(pref)) {
      return [pref];
    }
  } catch {
    // Ignore invalid browser storage and fall back to the default listing.
  }
  return [...DEFAULT_SORT];
}

function writeSortPreference(pref) {
  try {
    localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(pref));
  } catch {
    // Sorting still works for the current page when storage is unavailable.
  }
}

function clearSortPreference() {
  try {
    localStorage.removeItem(SORT_STORAGE_KEY);
  } catch {
    // Ignore locked-down browser storage.
  }
}

function rowSortValue(row, key) {
  if (key === "name") return row.dataset.sortName || row.dataset.name || "";
  const value = Number(row.dataset[`sort${key[0].toUpperCase()}${key.slice(1)}`]);
  return Number.isFinite(value) ? value : 0;
}

function compareRowsByEntry(a, b, entry) {
  const aValue = rowSortValue(a, entry.key);
  const bValue = rowSortValue(b, entry.key);
  const result = typeof aValue === "string"
    ? collator.compare(aValue, String(bValue))
    : aValue - Number(bValue);
  return entry.dir === "asc" ? result : -result;
}

function compareRows(a, b, sortEntries) {
  const aDir = a.dataset.isDir === "true";
  const bDir = b.dataset.isDir === "true";
  if (aDir !== bDir) return aDir ? -1 : 1;

  for (const entry of sortEntries) {
    const result = compareRowsByEntry(a, b, entry);
    if (result !== 0) return result;
  }
  return collator.compare(a.dataset.sortName || "", b.dataset.sortName || "");
}

function updateSortUi(sortEntries) {
  sortHeaders.forEach(btn => {
    const index = sortEntries.findIndex(entry => entry.key === btn.dataset.sortKey);
    const entry = sortEntries[index];
    const active = Boolean(entry);
    const th = btn.closest("th");
    const indicator = btn.querySelector(".sort-indicator");
    btn.classList.toggle("active", active);
    if (indicator) {
      indicator.textContent = active
        ? `${entry.dir === "asc" ? "▲" : "▼"}${sortEntries.length > 1 ? index + 1 : ""}`
        : "";
    }
    if (th) th.setAttribute("aria-sort", active ? (entry.dir === "asc" ? "ascending" : "descending") : "none");
  });
}

function applySort(sortEntries) {
  const tbody = document.querySelector(".file-table tbody");
  if (!tbody) return;
  const rows = [...tbody.querySelectorAll(".selectable-entry")].sort((a, b) => compareRows(a, b, sortEntries));
  rows.forEach(row => tbody.appendChild(row));
  tableSelection?.refreshRows();
  updateSortUi(sortEntries);
}

let currentSort = readSortPreference();
applySort(currentSort);

sortHeaders.forEach(btn => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.sortKey;
    const existing = currentSort.find(entry => entry.key === key);
    if (!existing) {
      currentSort = [
        ...currentSort,
        { key, dir: "asc" },
      ];
    } else if (existing.dir === "asc") {
      currentSort = currentSort.map(entry => (
        entry.key === key ? { key, dir: "desc" } : entry
      ));
    } else {
      currentSort = currentSort.filter(entry => entry.key !== key);
    }
    writeSortPreference(currentSort);
    tableSelection?.clearSelection();
    applySort(currentSort);
  });
});

resetSortBtn.addEventListener("click", () => {
  currentSort = [...DEFAULT_SORT];
  clearSortPreference();
  tableSelection?.clearSelection();
  applySort(currentSort);
});

async function restoreDelete(transactionId) {
  const res = await authFetch(`/api/restore/${transactionId}`, { method: "POST" });
  if (!res.ok) {
    dialogs.toast("Could not restore deleted items.", "error");
    return;
  }
  const data = await res.json();
  dialogs.toast(`${data.restored || 0} item${data.restored === 1 ? "" : "s"} restored. Refreshing...`, "success");
  setTimeout(() => location.reload(), 500);
}

async function showUndoDeleteToast(res, paths) {
  const data = await res.json();
  const count = data.count || data.deleted || paths.length;
  tableSelection?.removeRowsAndFocus(paths, filesRegion);
  updateItemCount();
  renderEmptyFolderState();
  dialogs.toastWithAction(
    `${count} item${count === 1 ? "" : "s"} deleted`,
    "Undo",
    () => restoreDelete(data.transaction_id),
    "undo",
    7000,
  );
}

async function downloadSelectedZip(paths = [...(tableSelection?.selectedPaths ?? [])]) {
  if (!paths.length) return;
  zipOverlay.setBusy(paths.length);
  let res;
  try {
    res = await authFetch("/_bulk/zip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base: CURRENT_PATH, paths }),
    });
  } finally {
    zipOverlay.setBusy(0);
  }
  if (!res.ok) {
    await dialogs.alert("Zip download failed", `Server returned ${res.status}.`);
    return;
  }
  const blob = await res.blob();
  const filename = filenameFromContentDisposition(res.headers.get("Content-Disposition")) || "xwing-selection.zip";
  downloadBlob(blob, filename);
}

zipSelectedBtn.addEventListener("click", () => downloadSelectedZip());

deleteSelectedBtn.addEventListener("click", async () => {
  if (!CAN_DELETE) {
    warnReadOnly("delete");
    return;
  }
  if (!tableSelection?.selectedPaths.size) return;
  const names = tableSelection.selectedRows()
    .map(row => row.dataset.name);
  const preview = names.slice(0, 6).join("\n");
  const extra = names.length > 6 ? `\n…and ${names.length - 6} more` : "";
  const confirmed = await dialogs.confirm(
    `Delete ${names.length} selected item${names.length === 1 ? "" : "s"}?`,
    `${preview}${extra}`,
    "Delete",
  );
  if (!confirmed) return;
  const res = await authFetch("/_bulk/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths: [...tableSelection.selectedPaths] }),
  });
  if (res.ok) await showUndoDeleteToast(res, [...tableSelection.selectedPaths]);
  else await dialogs.alert("Delete failed", `Server returned ${res.status}.`);
});

tableSelection = wireFileTableSelection({
  table: fileTable,
  selectAll,
  clearSelectionBtn,
  zipSelectedBtn,
  deleteSelectedBtn,
  selectionCount,
  tableWrap,
  canDelete: CAN_DELETE,
  onOpen(link) {
    link.click();
  },
  onDelete(row) {
    const button = row.querySelector(".btn-delete:not(:disabled)");
    button?.click();
  },
  onDeleteSelected() {
    deleteSelectedBtn.click();
  },
});

// ── Delete ─────────────────────────────────────────────────────────────────────
document.querySelectorAll(".btn-delete").forEach(btn => {
  btn.addEventListener("click", async () => {
    if (!CAN_DELETE) {
      warnReadOnly("delete");
      return;
    }
    const path = btn.dataset.path;
    const name = path.replace(/\/$/, "").split("/").pop();
    if (!await dialogs.confirm("Delete item?", `Delete "${name}"?`, "Delete")) return;
    const res = await authFetch(path, { method: "DELETE" });
    if (res.ok) await showUndoDeleteToast(res, [path]);
    else await dialogs.alert("Delete failed", `Server returned ${res.status}.`);
  });
});

// ── New folder ─────────────────────────────────────────────────────────────────
document.getElementById("mkdir-btn").addEventListener("click", async () => {
  if (!CAN_WRITE) {
    warnReadOnly("folder creation");
    return;
  }
  const name = await dialogs.prompt("New folder", "Create a folder in the current directory.", "Folder name");
  if (!name) return;
  const path = appendPath(CURRENT_PATH, name);
  const res = await authFetch(path, { method: "MKCOL" });
  if (res.ok || res.status === 201) location.reload();
  else await dialogs.alert("Could not create folder", `Server returned ${res.status}.`);
});

// ── Upload panel ───────────────────────────────────────────────────────────────
const panel = document.getElementById("upload-panel");
const uploadList = document.getElementById("upload-list");
let pendingReload = false;

document.getElementById("close-panel").addEventListener("click", () => {
  panel.classList.add("hidden");
  document.body.classList.remove("upload-panel-open");
  uploadList.innerHTML = "";
  renderUploadEmptyState();
  if (pendingReload) location.reload();
});

function renderUploadEmptyState() {
  if (uploadList.children.length) return;
  const empty = document.createElement("div");
  empty.className = "upload-panel-empty";
  empty.textContent = "No active uploads";
  uploadList.appendChild(empty);
}

function showPanel() {
  panel.classList.remove("hidden");
  document.body.classList.add("upload-panel-open");
  renderUploadEmptyState();
}

function addUploadItem(name) {
  uploadList.querySelector(".upload-panel-empty")?.remove();
  const item = document.createElement("div");
  item.className = "upload-item";

  const nameEl = document.createElement("div");
  nameEl.className = "upload-item-name";
  nameEl.title = name;
  nameEl.textContent = name;

  const progressWrap = document.createElement("div");
  progressWrap.className = "progress-bar-wrap";

  const progressBar = document.createElement("div");
  progressBar.className = "progress-bar";
  progressBar.style.width = "0%";
  progressWrap.appendChild(progressBar);

  const status = document.createElement("div");
  status.className = "upload-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  status.textContent = "Preparing upload...";

  item.append(nameEl, progressWrap, status);
  uploadList.appendChild(item);
  uploadList.scrollTop = uploadList.scrollHeight;
  return {
    setProgress(pct, speed) {
      progressBar.style.width = pct + "%";
      status.textContent = speed
        ? Math.round(pct) + "% (" + speed + ")"
        : Math.round(pct) + "%";
    },
    setStatus(msg) {
      status.textContent = msg;
    },
    setProcessing(on) {
      item.classList.toggle("processing", on);
    },
    setDone(msg = "Done") {
      item.classList.remove("processing");
      item.classList.add("done");
      progressBar.style.width = "100%";
      status.textContent = msg;
    },
    setError(msg) {
      item.classList.remove("processing");
      item.classList.add("error");
      status.textContent = "Error: " + msg;
    },
  };
}

// ── Directory helpers ─────────────────────────────────────────────────────────
const ensureDir = (path) => ensureDirWithFetch(path, authFetch);

async function prepareFolderRoot(name) {
  const finalRoot = appendPath(CURRENT_PATH, name);
  const created = await ensureDir(finalRoot);
  if (created) {
    return { uploadRoot: finalRoot, finalRoot, staged: false };
  }

  const replace = await dialogs.confirm(
    "Replace existing folder?",
    `A folder named "${name}" already exists. Upload into a staging folder and replace it when complete?`,
    "Replace",
  );
  if (!replace) {
    return null;
  }

  const uploadRoot = appendPath(CURRENT_PATH, stagingFolderName(name));
  await ensureDir(uploadRoot);
  return { uploadRoot, finalRoot, staged: true };
}

async function finishFolderRoot(root) {
  if (!root || !root.staged) return true;
  const res = await authFetch(root.uploadRoot, {
    method: "MOVE",
    headers: {
      "Destination": root.finalRoot,
      "Overwrite": "T",
    },
  });
  if (res.ok || res.status === 201 || res.status === 204) return true;

  try {
    await authFetch(root.uploadRoot, { method: "DELETE" });
  } catch {
    if (auth.isRedirecting()) return false;
    // Best-effort cleanup; leave the staging folder visible if delete fails.
  }
  await dialogs.alert("Could not replace folder", `Server returned ${res.status}.`);
  return false;
}

function putBlob(url, blob, callbacks = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    let uploadComplete = false;
    let stallTimer = null;
    let responseTimer = null;

    const clearTimers = () => {
      clearTimeout(stallTimer);
      clearTimeout(responseTimer);
    };
    const fail = message => {
      if (settled) return;
      settled = true;
      clearTimers();
      reject(new Error(message));
    };
    const armStallTimer = () => {
      clearTimeout(stallTimer);
      if (blob.size === 0) return;
      stallTimer = setTimeout(() => {
        if (!settled && !uploadComplete) callbacks.onStalled?.();
      }, UPLOAD_STALL_MS);
    };

    xhr.upload.addEventListener("loadstart", () => {
      callbacks.onStart?.();
      armStallTimer();
    });
    xhr.upload.addEventListener("progress", event => {
      const loaded = Math.min(blob.size, event.loaded);
      callbacks.onProgress?.(loaded, event.lengthComputable ? event.total : blob.size);
      if (loaded >= blob.size) {
        clearTimeout(stallTimer);
      } else {
        armStallTimer();
      }
    });
    xhr.upload.addEventListener("load", () => {
      uploadComplete = true;
      clearTimeout(stallTimer);
      callbacks.onProgress?.(blob.size, blob.size);
      callbacks.onUploaded?.();
      responseTimer = setTimeout(() => {
        if (!settled) callbacks.onResponseWait?.();
      }, UPLOAD_STALL_MS);
    });

    xhr.addEventListener("load", () => {
      if (settled) return;
      settled = true;
      clearTimers();
      if (xhr.status === 401 || isLoginResponseUrl(xhr.responseURL)) {
        auth.redirectToLogin();
        reject(new Error("authentication required"));
      } else if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr);
      } else {
        reject(httpStatusError(xhr.status));
      }
    });
    xhr.addEventListener("error", () => fail("network error"));
    xhr.addEventListener("abort", () => fail("aborted"));
    xhr.addEventListener("timeout", () => fail("timeout"));

    xhr.open("PUT", url, true);
    xhr.send(blob);
  });
}

class SpeedTracker {
  constructor() {
    this.samples = [];
    this.windowMs = 5000;
    this.maxSamples = 20;
  }
  sample(bytes) {
    const now = Date.now();
    this.samples = this.samples.filter(sample => now - sample.t <= this.windowMs);
    this.samples.push({ t: now, b: bytes });
    if (this.samples.length > this.maxSamples) {
      this.samples = this.samples.slice(this.samples.length - this.maxSamples);
    }
  }
  speed() {
    if (this.samples.length < 2) return 0;
    const first = this.samples[0];
    const last = this.samples[this.samples.length - 1];
    const elapsed = (last.t - first.t) / 1000;
    if (elapsed <= 0) return 0;
    return (last.b - first.b) / elapsed;
  }
  reset() {
    this.samples = [];
  }
}

function formatSpeed(bytesPerSecond) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytesPerSecond > 0 ? bytesPerSecond : 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return (unit ? value.toFixed(1) : Math.round(value)) + " " + units[unit] + "/s";
}

// ── Core: upload a single File to destDir on the server ───────────────────────
async function uploadFile(file, destDir) {
  const label = destDir !== CURRENT_PATH
    ? destDir.slice(CURRENT_PATH.length) + file.name
    : file.name;
  const ui = addUploadItem(label);
  showPanel();
  await nextPaint();

  const chunkSize = chunkSizeForFile(file.size);
  const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));

  let sessionId;
  try {
    ui.setStatus("Creating upload session...");
    const res = await withUploadRetries(async () => {
      const initRes = await authFetch("/_upload/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, total_chunks: totalChunks, chunk_size: chunkSize, dir: destDir }),
      });
      if (!initRes.ok) throw httpStatusError(initRes.status, "init failed " + initRes.status);
      return initRes;
    }, { label: "Upload session", ui });
    const init = await res.json();
    if (init.ignored) {
      ui.setDone("Skipped system file");
      return false;
    }
    ({ session_id: sessionId } = init);
  } catch (e) {
    if (auth.isRedirecting()) return false;
    ui.setError(e.message);
    return false;
  }

  const concurrency = getConcurrency();
  const chunkLoaded = new Array(totalChunks).fill(0);
  let loadedBytes = 0;
  const speedTracker = new SpeedTracker();

  function noteChunkLoaded(i, loaded, chunkSize) {
    const nextLoaded = Math.max(chunkLoaded[i], Math.min(chunkSize, loaded));
    loadedBytes += nextLoaded - chunkLoaded[i];
    chunkLoaded[i] = nextLoaded;
    const pct = file.size === 0
      ? UPLOAD_PROGRESS_CAP
      : Math.min(UPLOAD_PROGRESS_CAP, (loadedBytes / file.size) * UPLOAD_PROGRESS_CAP);
    speedTracker.sample(loadedBytes);
    ui.setProgress(pct, formatSpeed(speedTracker.speed()));
  }

  async function uploadChunk(i) {
    const start = i * chunkSize;
    const slice = file.slice(start, start + chunkSize);
    try {
      await withUploadRetries(() => putBlob(`/_upload/${sessionId}/${i}`, slice, {
        onStart() {
          ui.setProcessing(false);
          ui.setStatus(`Starting chunk ${i + 1}/${totalChunks}...`);
        },
        onProgress(loaded) {
          ui.setProcessing(false);
          noteChunkLoaded(i, loaded, slice.size);
        },
        onStalled() {
          ui.setProcessing(true);
          ui.setStatus("Processing upload...");
        },
        onUploaded() {
          ui.setProcessing(true);
          noteChunkLoaded(i, slice.size, slice.size);
          ui.setStatus("Waiting for server response...");
        },
        onResponseWait() {
          ui.setProcessing(true);
          ui.setStatus("Still processing...");
        },
      }), { label: `Chunk ${i + 1}/${totalChunks}`, ui });
      noteChunkLoaded(i, slice.size, slice.size);
    } finally {
      ui.setProcessing(false);
    }
  }

  try {
    let nextChunk = 0;
    async function uploadWorker() {
      while (nextChunk < totalChunks) {
        const chunkIndex = nextChunk;
        nextChunk += 1;
        await uploadChunk(chunkIndex);
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(concurrency, totalChunks); i++) {
      workers.push(uploadWorker());
    }
    await Promise.all(workers);
  } catch (e) {
    if (auth.isRedirecting()) return false;
    ui.setError(e.message);
    return false;
  }

  try {
    ui.setStatus("Finalizing...");
    await withUploadRetries(async () => {
      const res = await authFetch(`/_upload/${sessionId}/complete`, { method: "POST" });
      if (!res.ok) throw httpStatusError(res.status, "complete → " + res.status);
      return res;
    }, { label: "Finalizing upload", ui });
  } catch (e) {
    if (auth.isRedirecting()) return false;
    ui.setError(e.message);
    return false;
  }

  ui.setDone();
  return true;
}

// ── Upload a list of {file, destDir} pairs — N files in parallel ──────────────
async function uploadPairs(pairs, { reload = true } = {}) {
  let anySuccess = false;
  const concurrency = getConcurrency();

  // Process files in batches of `concurrency`
  for (let i = 0; i < pairs.length; i += concurrency) {
    const batch = pairs.slice(i, i + concurrency).map(async ({ file, destDir }) => {
      const ok = await uploadFile(file, destDir);
      if (ok) anySuccess = true;
    });
    await Promise.all(batch);
  }

  if (anySuccess && reload) {
    pendingReload = true;
    dialogs.toast("Upload complete. Refreshing file list...", "success");
    setTimeout(() => location.reload(), 600);
  }
  return anySuccess;
}

// ── Handle flat File[] from input[type=file] ──────────────────────────────────
async function uploadFlatFiles(files) {
  if (!CAN_WRITE) {
    warnReadOnly("uploads");
    return;
  }
  const pairs = [...files].map(f => ({ file: f, destDir: CURRENT_PATH }));
  await uploadPairs(pairs);
}

// ── Handle folder from input[webkitdirectory] ─────────────────────────────────
async function uploadFolderInput(files) {
  if (!CAN_WRITE) {
    warnReadOnly("uploads");
    return;
  }
  if (!files.length) return;
  // files[0].webkitRelativePath gives "folderName/sub/file.txt"
  const rootName = files[0].webkitRelativePath.split("/")[0];
  const root = await prepareFolderRoot(rootName);
  if (!root) return;

  const pairs = [];
  for (const file of files) {
    const parts = file.webkitRelativePath.split("/");
    // Build intermediate dirs
    let cur = root.uploadRoot;
    for (let i = 0; i < parts.length - 1; i++) {
      if (i > 0) {
        cur = appendPath(cur, parts[i]);
        await ensureDir(cur);
      }
    }
    pairs.push({ file, destDir: cur });
  }
  const ok = await uploadPairs(pairs, { reload: false });
  if (ok && await finishFolderRoot(root)) {
    pendingReload = true;
    dialogs.toast("Folder upload complete. Refreshing file list...", "success");
    setTimeout(() => location.reload(), 600);
  }
}

// ── Handle DataTransfer items (supports folders via FileSystem API) ────────────
async function uploadDataTransfer(dt) {
  if (!CAN_WRITE) {
    warnReadOnly("uploads");
    return;
  }
  const items = [...dt.items].filter(i => i.kind === "file");
  if (!items.length) return;

  const pairs = [];
  for (const item of items) {
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry && entry.isDirectory) {
      const root = await prepareFolderRoot(entry.name);
      if (!root) continue;
      const sub = await collectEntries(entry, root.uploadRoot, ensureDir);
      pairs.push(...sub);
      pairs.push({ folderRoot: root });
    } else {
      const file = item.getAsFile();
      if (file) pairs.push({ file, destDir: CURRENT_PATH });
    }
  }

  const folderRoots = pairs.filter(p => p.folderRoot).map(p => p.folderRoot);
  const filePairs = pairs.filter(p => p.file);
  const ok = await uploadPairs(filePairs, { reload: false });
  let finished = true;
  for (const root of folderRoots) {
    finished = await finishFolderRoot(root) && finished;
  }
  if ((ok || folderRoots.length) && finished) {
    pendingReload = true;
    dialogs.toast("Upload complete. Refreshing file list...", "success");
    setTimeout(() => location.reload(), 600);
  }
}

// ── File input buttons ─────────────────────────────────────────────────────────
const fileInput = document.getElementById("file-input");
const folderInput = document.getElementById("folder-input");

document.getElementById("upload-btn").addEventListener("click", () => {
  if (!CAN_WRITE) {
    warnReadOnly("uploads");
    return;
  }
  fileInput.click();
});
document.getElementById("upload-folder-btn").addEventListener("click", () => {
  if (!CAN_WRITE) {
    warnReadOnly("uploads");
    return;
  }
  folderInput.click();
});

document.addEventListener("click", event => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-empty-action]")
    : null;
  if (!button) return;
  if (button.dataset.emptyAction === "upload") {
    document.getElementById("upload-btn").click();
  } else if (button.dataset.emptyAction === "mkdir") {
    document.getElementById("mkdir-btn").click();
  }
});

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) uploadFlatFiles(fileInput.files);
  fileInput.value = "";
});

folderInput.addEventListener("change", () => {
  if (folderInput.files.length) uploadFolderInput(folderInput.files);
  folderInput.value = "";
});

// ── Drag and drop ──────────────────────────────────────────────────────────────
const dropZone = document.getElementById("drop-zone");
let dragCounter = 0;

dropZone.addEventListener("dragenter", e => {
  e.preventDefault();
  dragCounter++;
  dropZone.classList.add("dragging");
});

dropZone.addEventListener("dragleave", () => {
  dragCounter--;
  if (dragCounter === 0) dropZone.classList.remove("dragging");
});

dropZone.addEventListener("dragover", e => e.preventDefault());

dropZone.addEventListener("drop", async e => {
  e.preventDefault();
  dragCounter = 0;
  dropZone.classList.remove("dragging");
  await uploadDataTransfer(e.dataTransfer);
});

export { createZipOverlay, downloadSelectedZip };
