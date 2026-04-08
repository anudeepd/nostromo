"use strict";

const CHUNK_SIZE = 8 * 1024 * 1024;  // 8 MB

function getConcurrency() {
  return parseInt(document.getElementById("concurrency-select").value, 10) || 4;
}

// ── Date formatting ────────────────────────────────────────────────────────────
document.querySelectorAll("[data-mtime]").forEach(td => {
  const ts = parseFloat(td.dataset.mtime);
  if (!isNaN(ts)) {
    td.textContent = new Date(ts * 1000).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }
});

// ── Delete ─────────────────────────────────────────────────────────────────────
document.querySelectorAll(".btn-delete").forEach(btn => {
  btn.addEventListener("click", async () => {
    const path = btn.dataset.path;
    const name = path.replace(/\/$/, "").split("/").pop();
    if (!confirm(`Delete "${name}"?`)) return;
    const res = await fetch(path, { method: "DELETE" });
    if (res.ok) location.reload();
    else alert("Delete failed: " + res.status);
  });
});

// ── New folder ─────────────────────────────────────────────────────────────────
document.getElementById("mkdir-btn").addEventListener("click", async () => {
  const name = prompt("Folder name:");
  if (!name) return;
  const path = CURRENT_PATH + name;
  const res = await fetch(path, { method: "MKCOL" });
  if (res.ok || res.status === 201) location.reload();
  else alert("Could not create folder: " + res.status);
});

// ── Upload panel ───────────────────────────────────────────────────────────────
const panel = document.getElementById("upload-panel");
const uploadList = document.getElementById("upload-list");
let pendingReload = false;

document.getElementById("close-panel").addEventListener("click", () => {
  panel.classList.add("hidden");
  uploadList.innerHTML = "";
  if (pendingReload) location.reload();
});

function showPanel() {
  panel.classList.remove("hidden");
}

function addUploadItem(name) {
  const item = document.createElement("div");
  item.className = "upload-item";
  item.innerHTML = `
    <div class="upload-item-name" title="${name}">${name}</div>
    <div class="progress-bar-wrap"><div class="progress-bar" style="width:0%"></div></div>
    <div class="upload-status">0%</div>
  `;
  uploadList.appendChild(item);
  uploadList.scrollTop = uploadList.scrollHeight;
  return {
    setProgress(pct) {
      item.querySelector(".progress-bar").style.width = pct + "%";
      item.querySelector(".upload-status").textContent = Math.round(pct) + "%";
    },
    setDone() {
      item.classList.add("done");
      item.querySelector(".progress-bar").style.width = "100%";
      item.querySelector(".upload-status").textContent = "Done";
    },
    setError(msg) {
      item.classList.add("error");
      item.querySelector(".upload-status").textContent = "Error: " + msg;
    },
  };
}

// ── Ensure remote directory exists (MKCOL, tolerates 405 if already there) ────
async function ensureDir(serverPath) {
  const res = await fetch(serverPath, { method: "MKCOL" });
  if (!res.ok && res.status !== 405 && res.status !== 301) {
    throw new Error(`MKCOL ${serverPath} failed: ${res.status}`);
  }
}

// ── Core: upload a single File to destDir on the server ───────────────────────
async function uploadFile(file, destDir) {
  const label = destDir !== CURRENT_PATH
    ? destDir.slice(CURRENT_PATH.length) + file.name
    : file.name;
  const ui = addUploadItem(label);
  showPanel();

  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));

  let sessionId;
  try {
    const res = await fetch("/_upload/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, total_chunks: totalChunks, dir: destDir }),
    });
    if (!res.ok) throw new Error("init failed " + res.status);
    ({ session_id: sessionId } = await res.json());
  } catch (e) {
    ui.setError(e.message);
    return false;
  }

  let done = 0;
  const concurrency = getConcurrency();

  async function uploadChunk(i) {
    const start = i * CHUNK_SIZE;
    const slice = file.slice(start, start + CHUNK_SIZE);
    const res = await fetch(`/_upload/${sessionId}/${i}`, {
      method: "PUT",
      headers: { "Content-Length": String(slice.size) },
      body: slice,
    });
    if (!res.ok) throw new Error(`chunk ${i} → ${res.status}`);
    done++;
    ui.setProgress((done / totalChunks) * 95);
  }

  try {
    for (let i = 0; i < totalChunks; i += concurrency) {
      const batch = [];
      for (let j = i; j < Math.min(i + concurrency, totalChunks); j++) {
        batch.push(uploadChunk(j));
      }
      await Promise.all(batch);
    }
  } catch (e) {
    ui.setError(e.message);
    return false;
  }

  try {
    const res = await fetch(`/_upload/${sessionId}/complete`, { method: "POST" });
    if (!res.ok) throw new Error("complete → " + res.status);
  } catch (e) {
    ui.setError(e.message);
    return false;
  }

  ui.setDone();
  return true;
}

// ── Folder traversal via FileSystem API ───────────────────────────────────────
async function collectEntries(dirEntry, serverBase) {
  // Returns [{file, destDir}]
  const results = [];
  const reader = dirEntry.createReader();

  async function readAll() {
    return new Promise((resolve, reject) => {
      let all = [];
      function batch() {
        reader.readEntries(entries => {
          if (!entries.length) return resolve(all);
          all = all.concat([...entries]);
          batch();
        }, reject);
      }
      batch();
    });
  }

  const entries = await readAll();
  for (const entry of entries) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      results.push({ file, destDir: serverBase });
    } else if (entry.isDirectory) {
      const childBase = serverBase + entry.name + "/";
      await ensureDir(childBase);
      const sub = await collectEntries(entry, childBase);
      results.push(...sub);
    }
  }
  return results;
}

// ── Upload a list of {file, destDir} pairs — N files in parallel ──────────────
async function uploadPairs(pairs) {
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

  if (anySuccess) {
    pendingReload = true;
    setTimeout(() => location.reload(), 600);
  }
}

// ── Handle flat File[] from input[type=file] ──────────────────────────────────
async function uploadFlatFiles(files) {
  const pairs = [...files].map(f => ({ file: f, destDir: CURRENT_PATH }));
  await uploadPairs(pairs);
}

// ── Handle folder from input[webkitdirectory] ─────────────────────────────────
async function uploadFolderInput(files) {
  if (!files.length) return;
  // files[0].webkitRelativePath gives "folderName/sub/file.txt"
  const rootName = files[0].webkitRelativePath.split("/")[0];
  const rootServer = CURRENT_PATH + rootName + "/";
  await ensureDir(rootServer);

  const pairs = [];
  for (const file of files) {
    const parts = file.webkitRelativePath.split("/");
    // Build intermediate dirs
    let cur = CURRENT_PATH;
    for (let i = 0; i < parts.length - 1; i++) {
      cur += parts[i] + "/";
      if (i > 0) await ensureDir(cur);  // root already created above
    }
    pairs.push({ file, destDir: cur });
  }
  await uploadPairs(pairs);
}

// ── Handle DataTransfer items (supports folders via FileSystem API) ────────────
async function uploadDataTransfer(dt) {
  const items = [...dt.items].filter(i => i.kind === "file");
  if (!items.length) return;

  const pairs = [];
  for (const item of items) {
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry && entry.isDirectory) {
      const serverBase = CURRENT_PATH + entry.name + "/";
      await ensureDir(serverBase);
      const sub = await collectEntries(entry, serverBase);
      pairs.push(...sub);
    } else {
      const file = item.getAsFile();
      if (file) pairs.push({ file, destDir: CURRENT_PATH });
    }
  }
  await uploadPairs(pairs);
}

// ── File input buttons ─────────────────────────────────────────────────────────
const fileInput = document.getElementById("file-input");
const folderInput = document.getElementById("folder-input");

document.getElementById("upload-btn").addEventListener("click", () => fileInput.click());
document.getElementById("upload-folder-btn").addEventListener("click", () => folderInput.click());

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
