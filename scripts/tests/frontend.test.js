import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

import { createDialogController, wireFileTableSelection } from "../../xwing/frontend/src/app-core.js";
import { createAuthSession, loginUrlForCurrentPage } from "../../xwing/frontend/src/shared.js";

describe("shared auth helpers", () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="auth-overlay" hidden>
        <h2 id="auth-overlay-title"></h2>
        <p id="auth-overlay-message"></p>
      </div>
    `;
  });

  it("builds a login redirect for the current page", () => {
    const url = loginUrlForCurrentPage({
      pathname: "/folder/file.txt",
      search: "?edit",
      hash: "#line-4",
    });

    expect(url).toBe("/_auth/login?redirect=%2Ffolder%2Ffile.txt%3Fedit%23line-4");
  });

  it("shows the auth overlay and redirects on auth challenges", async () => {
    const assign = vi.fn();
    const session = createAuthSession({
      documentRef: document,
      fetchRef: vi.fn(async () => ({ status: 401, url: "http://xwing.local/private" })),
      windowRef: {
        location: {
          pathname: "/private",
          search: "",
          hash: "",
          href: "http://xwing.local/private",
          assign,
        },
        setTimeout: fn => fn(),
        clearTimeout: vi.fn(),
        addEventListener: vi.fn(),
      },
    });

    await expect(session.authFetch("/private")).rejects.toThrow("authentication required");
    expect(document.getElementById("auth-overlay").hidden).toBe(false);
    expect(document.getElementById("auth-overlay-title").textContent).toBe("Session expired");
    expect(assign).toHaveBeenCalledWith("/_auth/login?redirect=%2Fprivate");
  });

  it("expires instead of resetting when background timer delivery is delayed", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const session = createAuthSession({
      documentRef: document,
      windowRef: window,
      idleTimeoutSeconds: 2,
      redirectDelayMs: 10_000,
    });

    const cleanup = session.wireAuthIdleTimer();
    vi.setSystemTime(3_001);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "A" }));

    expect(session.isRedirecting()).toBe(true);
    cleanup();
    vi.useRealTimers();
  });

  it("checks an overdue auth deadline as soon as the page regains focus", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const session = createAuthSession({
      documentRef: document,
      windowRef: window,
      idleTimeoutSeconds: 2,
      redirectDelayMs: 10_000,
    });

    const cleanup = session.wireAuthIdleTimer();
    vi.setSystemTime(3_001);
    window.dispatchEvent(new Event("focus"));

    expect(session.isRedirecting()).toBe(true);
    cleanup();
    vi.useRealTimers();
  });
});

describe("xwing dialogs", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("resolves confirmations from owned dialog controls", async () => {
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.confirm("Delete item?", "Delete notes.txt?", "Delete");

    expect(document.querySelector(".xwing-dialog").textContent).toContain("Delete notes.txt?");
    document.querySelector(".btn-danger").click();

    await expect(result).resolves.toBe(true);
    expect(document.querySelector(".xwing-dialog")).toBeNull();
  });

  it("trims prompt input values", async () => {
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.prompt("New folder", "Create a folder.", "Folder name");

    document.querySelector(".xwing-dialog-input").value = "  reports  ";
    document.querySelector(".btn-primary").click();

    await expect(result).resolves.toBe("reports");
  });

  it("renders owned alerts and error toasts", async () => {
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.alert("Upload failed", "Could not upload report.pdf.");

    expect(document.querySelector(".xwing-dialog").textContent).toContain("Could not upload report.pdf.");
    expect(document.querySelectorAll(".xwing-dialog-actions .btn")).toHaveLength(1);
    expect(document.querySelector(".xwing-dialog-actions .btn").textContent).toBe("OK");
    document.querySelector(".btn-primary").click();
    await expect(result).resolves.toBe(true);

    dialogs.toast("Upload failed", "error");
    const toast = document.querySelector(".xwing-toast.error");
    expect(toast).not.toBeNull();
    expect(toast.getAttribute("role")).toBe("status");
    expect(toast.textContent).toBe("Upload failed");
  });

  it("resolves actionable toasts when the action is clicked", async () => {
    vi.useFakeTimers();
    const action = vi.fn();
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.toastWithAction("1 item deleted", "Undo", action, "undo", 7000);
    const button = document.querySelector(".toast-action");

    expect(document.getElementById("xwing-toast-stack").getAttribute("aria-live")).toBe("polite");
    expect(button.textContent).toBe("Undo");
    button.click();

    await expect(result).resolves.toBe(true);
    expect(action).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("resolves actionable toasts on timeout", async () => {
    vi.useFakeTimers();
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.toastWithAction("1 item deleted", "Undo", vi.fn(), "undo", 7000);

    vi.advanceTimersByTime(7000);

    await expect(result).resolves.toBe(false);
    vi.useRealTimers();
  });

  it("pauses toast countdown on hover and resumes on leave", async () => {
    vi.useFakeTimers();
    const dialogs = createDialogController({ documentRef: document });
    const result = dialogs.toastWithAction("1 item deleted", "Undo", vi.fn(), "undo", 7000);
    const toast = document.querySelector(".xwing-toast");
    const progress = toast.querySelector(".toast-progress");

    vi.advanceTimersByTime(2000);
    toast.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
    expect(progress.style.animationPlayState).toBe("paused");

    vi.advanceTimersByTime(9000);

    toast.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
    expect(progress.style.animationPlayState).toBe("running");

    vi.advanceTimersByTime(5000);
    await expect(result).resolves.toBe(false);
    vi.useRealTimers();
  });

  it("consumes Escape inside owned dialogs", async () => {
    const dialogs = createDialogController({ documentRef: document });
    const documentEscape = vi.fn();
    document.addEventListener("keydown", documentEscape);
    const result = dialogs.confirm("Discard unsaved changes?", "Leave without saving?", "Discard changes");
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });

    document.querySelector(".xwing-dialog-overlay").dispatchEvent(event);

    await expect(result).resolves.toBeNull();
    expect(event.defaultPrevented).toBe(true);
    expect(documentEscape).not.toHaveBeenCalled();
  });
});

describe("delegated file table selection", () => {
  function renderTable() {
    document.body.innerHTML = `
      <button id="zip-selected-btn" disabled>Download zip</button>
      <button id="delete-selected-btn" disabled>Delete selected</button>
      <button id="clear-selection-btn" disabled>Clear</button>
      <span id="selection-count" aria-hidden="true">0 selected</span>
      <main id="files-region" tabindex="-1"></main>
      <div class="table-wrap">
        <table class="file-table">
          <thead>
            <tr><th><input type="checkbox" id="select-all" /></th></tr>
          </thead>
          <tbody>
            <tr class="entry selectable-entry" data-path="/a.txt" data-name="a.txt">
              <td><input class="entry-select" type="checkbox" /></td>
              <td class="col-name"><a href="/a.txt">a.txt</a></td>
              <td><button class="btn-delete">delete</button></td>
            </tr>
            <tr class="entry selectable-entry" data-path="/b.txt" data-name="b.txt">
              <td><input class="entry-select" type="checkbox" /></td>
              <td class="col-name"><a href="/b.txt">b.txt</a></td>
              <td><button class="btn-delete">delete</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  }

  beforeEach(renderTable);

  it("selects and clears rows from delegated keyboard handlers", () => {
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      clearSelectionBtn: document.getElementById("clear-selection-btn"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      selectionCount: document.getElementById("selection-count"),
      tableWrap: document.querySelector(".table-wrap"),
      canDelete: true,
    });
    const firstRow = document.querySelector(".selectable-entry");

    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect(controller.selectedPaths.has("/a.txt")).toBe(true);
    expect(document.getElementById("zip-selected-btn").textContent).toBe("Download zip (1)");
    expect(document.getElementById("clear-selection-btn").disabled).toBe(false);
    expect(document.getElementById("selection-count").getAttribute("aria-hidden")).toBe("false");
    expect(document.getElementById("selection-count").textContent).toBe("1 selected");
    expect(document.querySelector(".table-wrap").classList.contains("selection-mode")).toBe(true);

    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(controller.selectedPaths.size).toBe(0);
    expect(document.getElementById("zip-selected-btn").disabled).toBe(true);
    expect(document.getElementById("clear-selection-btn").disabled).toBe(true);
    expect(document.getElementById("selection-count").getAttribute("aria-hidden")).toBe("true");
    expect(document.querySelector(".table-wrap").classList.contains("selection-mode")).toBe(false);
  });

  it("removes deleted rows and focuses the nearest survivor", () => {
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      clearSelectionBtn: document.getElementById("clear-selection-btn"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      selectionCount: document.getElementById("selection-count"),
      tableWrap: document.querySelector(".table-wrap"),
      canDelete: true,
    });
    const rows = document.querySelectorAll(".selectable-entry");

    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    controller.removeRowsAndFocus(["/a.txt"], document.getElementById("files-region"));

    expect(document.querySelector('[data-path="/a.txt"]')).toBeNull();
    expect(document.activeElement).toBe(document.querySelector('[data-path="/b.txt"]'));
    expect(controller.selectedPaths.size).toBe(0);
  });

  it("disables select-all after every selectable row is removed", () => {
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      clearSelectionBtn: document.getElementById("clear-selection-btn"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      selectionCount: document.getElementById("selection-count"),
      tableWrap: document.querySelector(".table-wrap"),
      canDelete: true,
    });

    controller.removeRowsAndFocus(["/a.txt", "/b.txt"], document.getElementById("files-region"));

    expect(document.querySelectorAll(".selectable-entry")).toHaveLength(0);
    expect(document.getElementById("select-all").disabled).toBe(true);
    expect(document.activeElement).toBe(document.getElementById("files-region"));
  });

  it("clears selections from the visible clear selection control", () => {
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      clearSelectionBtn: document.getElementById("clear-selection-btn"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      selectionCount: document.getElementById("selection-count"),
      tableWrap: document.querySelector(".table-wrap"),
      canDelete: true,
    });
    const firstRow = document.querySelector(".selectable-entry");
    const clearSelectionBtn = document.getElementById("clear-selection-btn");

    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect(controller.selectedPaths.size).toBe(1);
    expect(clearSelectionBtn.disabled).toBe(false);

    clearSelectionBtn.click();

    expect(controller.selectedPaths.size).toBe(0);
    expect(clearSelectionBtn.disabled).toBe(true);
    expect(document.querySelector(".table-wrap").classList.contains("selection-mode")).toBe(false);
  });

  it("toggles focused rows with Space without collapsing other selections", () => {
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
    });
    const rows = document.querySelectorAll(".selectable-entry");

    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    rows[1].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect([...controller.selectedPaths]).toEqual(["/a.txt", "/b.txt"]);

    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect([...controller.selectedPaths]).toEqual(["/b.txt"]);
    expect(document.getElementById("zip-selected-btn").textContent).toBe("Download zip (1)");
  });

  it("opens and deletes focused rows from delegated keyboard handlers", () => {
    const open = vi.fn();
    const deleteRow = vi.fn();
    wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
      onOpen: open,
      onDelete: deleteRow,
    });
    const firstRow = document.querySelector(".selectable-entry");

    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));

    expect(open).toHaveBeenCalledWith(document.querySelector(".col-name a"));
    expect(deleteRow).toHaveBeenCalledWith(firstRow);
  });

  it("deletes the full selection when Delete is pressed in selection mode", () => {
    const deleteRow = vi.fn();
    const deleteSelection = vi.fn();
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
      onDelete: deleteRow,
      onDeleteSelected: deleteSelection,
    });
    const rows = document.querySelectorAll(".selectable-entry");

    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    rows[1].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));

    expect([...controller.selectedPaths]).toEqual(["/a.txt", "/b.txt"]);
    expect(deleteSelection).toHaveBeenCalledOnce();
    expect(deleteRow).not.toHaveBeenCalled();
  });

  it("deletes the full selection when Backspace is pressed in selection mode", () => {
    const deleteSelection = vi.fn();
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
      onDeleteSelected: deleteSelection,
    });
    const rows = document.querySelectorAll(".selectable-entry");

    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    rows[1].dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: "Backspace", bubbles: true }));

    expect(controller.selectedPaths.size).toBe(2);
    expect(deleteSelection).toHaveBeenCalledOnce();
  });

  it("deletes the full selection when Delete is pressed on select-all", () => {
    const deleteSelection = vi.fn();
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
      onDeleteSelected: deleteSelection,
    });
    const selectAll = document.getElementById("select-all");

    selectAll.click();
    selectAll.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));

    expect(controller.selectedPaths.size).toBe(2);
    expect(deleteSelection).toHaveBeenCalledOnce();
  });

  it("clears selection with Escape after a toolbar delete dialog is dismissed", async () => {
    const dialogs = createDialogController({ documentRef: document });
    const controller = wireFileTableSelection({
      documentRef: document,
      table: document.querySelector(".file-table"),
      selectAll: document.getElementById("select-all"),
      zipSelectedBtn: document.getElementById("zip-selected-btn"),
      deleteSelectedBtn: document.getElementById("delete-selected-btn"),
      canDelete: true,
    });
    const firstRow = document.querySelector(".selectable-entry");
    const deleteSelectedBtn = document.getElementById("delete-selected-btn");

    firstRow.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect(controller.selectedPaths.size).toBe(1);

    deleteSelectedBtn.focus();
    const result = dialogs.confirm("Delete selected?", "Delete selected item?", "Delete");
    await Promise.resolve();
    document.querySelector(".xwing-dialog-overlay").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await expect(result).resolves.toBeNull();
    expect(controller.selectedPaths.size).toBe(1);
    expect(document.activeElement).toBe(deleteSelectedBtn);

    deleteSelectedBtn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(controller.selectedPaths.size).toBe(0);
    expect(deleteSelectedBtn.disabled).toBe(true);
  });
});

describe("responsive file browser styles", () => {
  it("wraps every toolbar group on narrow viewports", () => {
    const stylesheet = readFileSync("../xwing/frontend/src/style.css", "utf8");

    expect(stylesheet).toContain("@media (max-width: 700px)");
    expect(stylesheet).toContain(`.toolbar-primary,
  .toolbar-selection,
  .toolbar-meta {
    flex: 1 1 100%;
    flex-wrap: wrap;
  }`);
  });
});
