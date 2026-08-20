import { describe, expect, it, vi } from "vitest";

import collectEntries from "../../xwing/frontend/src/upload-entries.js";

function makeFileEntry(name, file) {
  return {
    name,
    isFile: true,
    isDirectory: false,
    file: cb => cb(file),
  };
}

function makeDirEntry(name, entries) {
  let calls = 0;
  const reader = {
    readEntries: vi.fn(cb => {
      cb(calls++ === 0 ? entries : []);
    }),
  };
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader: () => reader,
  };
}

describe("collectEntries (upload-entries.js)", () => {
  it("skips null entries while still processing files and recursing into directories", async () => {
    const file = { name: "a.txt" };
    const nestedFile = { name: "b.txt" };
    const subDir = makeDirEntry("sub", [makeFileEntry("b.txt", nestedFile)]);
    const root = makeDirEntry("root", [makeFileEntry("a.txt", file), null, subDir, null]);
    const ensureDir = vi.fn(async () => {});

    const results = await collectEntries(root, "/base/", ensureDir);

    expect(results).toEqual([
      { file, destDir: "/base/" },
      { file: nestedFile, destDir: "/base/sub/" },
    ]);
    expect(ensureDir).toHaveBeenCalledWith("/base/sub/");
  });

  it("returns an empty result when every entry is null", async () => {
    const root = makeDirEntry("root", [null, null, null]);

    const results = await collectEntries(root, "/base/", vi.fn(async () => {}));

    expect(results).toEqual([]);
  });

  it("counts only the valid file entries when nulls are mixed in", async () => {
    const files = [{ name: "a.txt" }, { name: "b.txt" }, { name: "c.txt" }];
    const root = makeDirEntry("root", [
      makeFileEntry("a.txt", files[0]),
      null,
      makeFileEntry("b.txt", files[1]),
      null,
      makeFileEntry("c.txt", files[2]),
    ]);

    const results = await collectEntries(root, "/base/", vi.fn(async () => {}));

    expect(results).toHaveLength(3);
    expect(results.map(r => r.file)).toEqual(files);
    expect(results.every(r => r.destDir === "/base/")).toBe(true);
  });
});