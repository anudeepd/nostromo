import { beforeEach, describe, expect, it, vi } from "vitest";

// Minimal DOM mirroring the server-rendered file browser shell that app.js
// queries at module load. The zip overlay itself is created by app.js at init.
function renderAppDom() {
  document.body.dataset.currentPath = "/";
  document.body.dataset.user = "tester";
  document.body.dataset.canWrite = "true";
  document.body.dataset.canDelete = "true";
  document.body.innerHTML = `
    <div id="auth-overlay" hidden></div>
    <button id="zip-selected-btn"></button>
    <button id="delete-selected-btn"></button>
    <button id="clear-selection-btn"></button>
    <span id="selection-count"></span>
    <main id="files-region"></main>
    <div class="table-wrap"><table class="file-table"><tbody></tbody></table></div>
    <input type="checkbox" id="select-all" />
    <button id="reset-sort-btn"></button>
    <div id="upload-panel"><div id="upload-list"></div></div>
    <button id="close-panel"></button>
    <button id="mkdir-btn"></button>
    <input id="file-input" />
    <input id="folder-input" />
    <button id="upload-btn"></button>
    <button id="upload-folder-btn"></button>
    <div id="drop-zone"></div>
    <div id="concurrency-select"></div>
  `;
}

// A fetch mock whose resolution we control, so the test can observe the
// overlay while the /_bulk/zip request is still in flight.
function deferredFetch() {
  let resolve;
  const fetchMock = vi.fn(() => new Promise(r => { resolve = r; }));
  return {
    fetchMock,
    resolve: value => resolve(value),
  };
}

describe("bulk zip spinner overlay", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.resetModules();
    renderAppDom();
  });

  it("shows the overlay with the selected count while the zip request is in flight", async () => {
    const { fetchMock, resolve } = deferredFetch();
    vi.stubGlobal("fetch", fetchMock);

    const { downloadSelectedZip } = await import("../../xwing/frontend/src/app.js");
    vi.useFakeTimers();
    const overlay = document.getElementById("zip-overlay");
    expect(overlay.hidden).toBe(true);

    const promise = downloadSelectedZip(["/a.txt", "/b.txt"]);

    expect(overlay.hidden).toBe(false);
    expect(document.getElementById("zip-overlay-text").textContent).toBe("Zipping 2 files…");
    expect(fetchMock).toHaveBeenCalledWith("/_bulk/zip", expect.objectContaining({ method: "POST" }));

    resolve({ ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(["zip"]) });
    await promise;
    await vi.advanceTimersByTimeAsync(160);
  });

  it("hides the overlay when the zip request succeeds", async () => {
    vi.useFakeTimers();
    const { fetchMock, resolve } = deferredFetch();
    vi.stubGlobal("fetch", fetchMock);

    const { downloadSelectedZip } = await import("../../xwing/frontend/src/app.js");
    const overlay = document.getElementById("zip-overlay");

    const promise = downloadSelectedZip(["/a.txt"]);
    expect(overlay.hidden).toBe(false);

    resolve({
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Disposition": "attachment; filename*=UTF-8''xwing.zip" }),
      blob: async () => new Blob(["zip"]),
    });
    await promise;
    await vi.advanceTimersByTimeAsync(160);

    expect(overlay.hidden).toBe(true);
  });

  it("hides the overlay when the zip request fails", async () => {
    vi.useFakeTimers();
    const { fetchMock, resolve } = deferredFetch();
    vi.stubGlobal("fetch", fetchMock);

    const { downloadSelectedZip } = await import("../../xwing/frontend/src/app.js");
    const overlay = document.getElementById("zip-overlay");

    const promise = downloadSelectedZip(["/a.txt"]);
    expect(overlay.hidden).toBe(false);

    resolve({ ok: false, status: 500, headers: new Headers() });
    // Flush the fetch resolution (overlay starts fading out, error alert shows),
    // then finish the 150ms fade-out.
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(160);

    expect(overlay.hidden).toBe(true);

    // Dismiss the error alert so the flow settles.
    document.querySelector(".xwing-dialog .btn-primary").click();
    await promise;
  });
});