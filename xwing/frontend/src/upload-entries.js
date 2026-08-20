// ── Folder traversal via FileSystem API ───────────────────────────────────────

export function appendPath(base, name) {
  return base + encodeURIComponent(name) + "/";
}

export async function ensureDir(serverPath, authFetch) {
  const res = await authFetch(serverPath, { method: "MKCOL" });
  if (!res.ok && res.status !== 405 && res.status !== 301) {
    throw new Error(`MKCOL ${serverPath} failed: ${res.status}`);
  }
  return res.status !== 405 && res.status !== 301;
}

async function readAll(reader) {
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

export default async function collectEntries(dirEntry, serverBase, ensureDirFn = ensureDir) {
  // Returns [{file, destDir}]
  const results = [];
  const reader = dirEntry.createReader();

  const entries = await readAll(reader);
  for (const entry of entries) {
    // Some browsers and enterprise extensions (e.g. ForcePoint, Menlo) can leave
    // null slots in the readEntries() result. Skip them defensively instead of
    // throwing "Cannot read properties of null (reading 'isFile')".
    if (!entry) continue;
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      results.push({ file, destDir: serverBase });
    } else if (entry.isDirectory) {
      const childBase = appendPath(serverBase, entry.name);
      await ensureDirFn(childBase);
      const sub = await collectEntries(entry, childBase, ensureDirFn);
      results.push(...sub);
    }
  }
  return results;
}