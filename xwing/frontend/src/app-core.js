const DELETE_KEYS = { Delete: true, Backspace: true };

export function createDialogController({ documentRef = document } = {}) {
  let activeResolve = null;
  let savedFocus = null;

  function ensureRoot() {
    let root = documentRef.getElementById("xwing-dialog-root");
    if (!root) {
      root = documentRef.createElement("div");
      root.id = "xwing-dialog-root";
      documentRef.body.appendChild(root);
    }
    return root;
  }

  function close(value) {
    const overlay = documentRef.querySelector(".xwing-dialog-overlay");
    if (overlay) overlay.remove();
    const resolve = activeResolve;
    activeResolve = null;
    if (resolve) resolve(value);
    const prev = savedFocus;
    savedFocus = null;
    if (prev && prev.isConnected) prev.focus();
  }

  function open({ title, message, kind = "default", promptLabel = "", promptValue = "", confirmText = "OK", cancelText = "Cancel" }) {
    if (activeResolve) close(null);
    savedFocus = documentRef.activeElement;
    const root = ensureRoot();
    const overlay = documentRef.createElement("div");
    overlay.className = "xwing-dialog-overlay";
    overlay.addEventListener("click", event => {
      if (event.target === overlay) close(kind === "alert" ? true : null);
    });
    overlay.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close(kind === "alert" ? true : null);
        return;
      }
      if (event.key === "Tab") {
        const focusable = [...dialog.querySelectorAll("button:not([disabled]), input:not([disabled])")];
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!first || !last) return;
        if (event.shiftKey && (documentRef.activeElement === first || !dialog.contains(documentRef.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && documentRef.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });

    const dialog = documentRef.createElement("form");
    dialog.className = "xwing-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "xwing-dialog-title");
    dialog.setAttribute("aria-describedby", "xwing-dialog-message");

    const heading = documentRef.createElement("h2");
    heading.id = "xwing-dialog-title";
    heading.textContent = title;

    const body = documentRef.createElement("p");
    body.id = "xwing-dialog-message";
    body.textContent = message;

    let input = null;
    if (kind === "prompt") {
      const label = documentRef.createElement("label");
      label.className = "xwing-dialog-label";
      label.textContent = promptLabel;
      input = documentRef.createElement("input");
      input.className = "xwing-dialog-input";
      input.type = "text";
      input.value = promptValue;
      label.appendChild(input);
      dialog.append(heading, body, label);
    } else {
      dialog.append(heading, body);
    }

    const actions = documentRef.createElement("div");
    actions.className = "xwing-dialog-actions";

    const cancel = documentRef.createElement("button");
    cancel.className = "btn";
    cancel.type = "button";
    cancel.textContent = cancelText;
    cancel.addEventListener("click", () => close(kind === "alert" ? true : null));

    const confirm = documentRef.createElement("button");
    confirm.className = kind === "danger" ? "btn btn-danger" : "btn btn-primary";
    confirm.type = "submit";
    confirm.textContent = confirmText;
    dialog.addEventListener("submit", event => {
      event.preventDefault();
      close(input ? input.value.trim() : true);
    });

    if (kind === "alert") actions.append(confirm);
    else actions.append(cancel, confirm);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    root.replaceChildren(overlay);

    return new Promise(resolve => {
      activeResolve = resolve;
      window.setTimeout(() => (input || confirm).focus(), 0);
    });
  }

  function toast(message, kind = "default") {
    toastWithAction(message, "", null, kind, 4200);
  }

  function toastWithAction(message, actionLabel = "", action = null, kind = "default", duration = 4200) {
    let stack = documentRef.getElementById("xwing-toast-stack");
    if (!stack) {
      stack = documentRef.createElement("div");
      stack.id = "xwing-toast-stack";
      stack.className = "xwing-toast-stack";
      stack.setAttribute("aria-live", "polite");
      documentRef.body.appendChild(stack);
    }
    const item = documentRef.createElement("div");
    item.className = `xwing-toast ${kind}`;
    item.setAttribute("role", "status");
    const messageEl = documentRef.createElement("span");
    messageEl.className = "toast-message";
    messageEl.textContent = message;
    item.appendChild(messageEl);

    let settled = false;
    let timer = null;
    const finish = value => {
      if (settled) return;
      settled = true;
      if (timer) window.clearTimeout(timer);
      item.classList.add("dismissing");
      window.setTimeout(() => item.remove(), 140);
      return value;
    };

    const promise = new Promise(resolve => {
      if (actionLabel && action) {
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = "toast-action";
        button.textContent = actionLabel;
        button.addEventListener("click", async () => {
          finish(true);
          resolve(true);
          await action();
        });
        item.appendChild(button);
      }

      if (duration > 0) {
        const progress = documentRef.createElement("span");
        progress.className = "toast-progress";
        progress.style.animationDuration = `${duration}ms`;
        item.appendChild(progress);

        let remaining = duration;
        let startedAt = Date.now();

        function startTimer() {
          startedAt = Date.now();
          timer = window.setTimeout(() => {
            finish(false);
            resolve(false);
          }, remaining);
        }

        item.addEventListener("mouseenter", () => {
          if (settled) return;
          remaining -= Date.now() - startedAt;
          if (timer) window.clearTimeout(timer);
          progress.style.animationPlayState = "paused";
        });

        item.addEventListener("mouseleave", () => {
          if (settled) return;
          progress.style.animationPlayState = "running";
          startTimer();
        });

        startTimer();
      }
    });

    stack.appendChild(item);
    return promise;
  }

  return {
    alert: (title, message) => open({ title, message, kind: "alert", confirmText: "OK" }),
    confirm: (title, message, confirmText = "Confirm") => open({ title, message, kind: "danger", confirmText }),
    prompt: (title, message, promptLabel, promptValue = "") => open({ title, message, kind: "prompt", promptLabel, promptValue, confirmText: "Create" }),
    toast,
    toastWithAction,
  };
}

export function wireFileTableSelection({
  documentRef = document,
  table,
  selectAll,
  clearSelectionBtn,
  zipSelectedBtn,
  deleteSelectedBtn,
  selectionCount,
  tableWrap,
  canDelete,
  onOpen,
  onDelete,
  onDeleteSelected,
} = {}) {
  const selectedPaths = new Set();
  let selectableRows = [];
  let lastSelectedIndex = null;

  function rowPath(row) {
    return row?.dataset.path;
  }

  function setRowSelected(row, selected) {
    const path = rowPath(row);
    if (!path) return;
    row.classList.toggle("selected", selected);
    row.setAttribute("aria-selected", selected ? "true" : "false");
    const checkbox = row.querySelector(".entry-select");
    if (checkbox) checkbox.checked = selected;
    if (selected) selectedPaths.add(path);
    else selectedPaths.delete(path);
  }

  function updateBulkActions() {
    const count = selectedPaths.size;
    if (zipSelectedBtn) {
      zipSelectedBtn.disabled = count === 0;
      zipSelectedBtn.textContent = count ? `Download zip (${count})` : "Download zip";
      zipSelectedBtn.title = count ? "Download selected files and folders as zip" : "Select files or folders first";
    }

    if (deleteSelectedBtn && canDelete) {
      deleteSelectedBtn.disabled = count === 0;
      deleteSelectedBtn.textContent = count ? `Delete selected (${count})` : "Delete selected";
      deleteSelectedBtn.title = count ? "Delete selected files and folders" : "Select files or folders first";
    }

    if (selectAll) {
      selectAll.checked = count > 0 && count === selectableRows.length;
      selectAll.indeterminate = count > 0 && count < selectableRows.length;
      selectAll.disabled = selectableRows.length === 0;
    }

    if (clearSelectionBtn) {
      clearSelectionBtn.disabled = count === 0;
    }

    if (selectionCount) {
      selectionCount.textContent = `${count} selected`;
      selectionCount.setAttribute("aria-hidden", count === 0 ? "true" : "false");
    }

    tableWrap?.classList.toggle("selection-mode", count > 0);
  }

  function clearSelection() {
    selectableRows.forEach(row => setRowSelected(row, false));
    lastSelectedIndex = null;
    updateBulkActions();
  }

  function refreshRows() {
    selectableRows = [...documentRef.querySelectorAll(".selectable-entry")];
    selectableRows.forEach(row => {
      row.tabIndex = 0;
      const isSelected = selectedPaths.has(rowPath(row));
      row.setAttribute("aria-selected", isSelected ? "true" : "false");
      const checkbox = row.querySelector(".entry-select");
      if (checkbox) checkbox.checked = isSelected;
    });
    updateBulkActions();
  }

  function selectRange(toIndex, selected) {
    const fromIndex = lastSelectedIndex === null ? toIndex : lastSelectedIndex;
    const start = Math.min(fromIndex, toIndex);
    const end = Math.max(fromIndex, toIndex);
    for (let i = start; i <= end; i++) setRowSelected(selectableRows[i], selected);
  }

  function handleRowSelection(row, event, forcedSelected = null, preserveOthers = false) {
    const index = selectableRows.indexOf(row);
    if (index < 0) return;
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    const nextSelected = forcedSelected ?? !selectedPaths.has(rowPath(row));

    if (event.shiftKey) {
      selectRange(index, nextSelected);
    } else if (preserveOthers || event.metaKey || event.ctrlKey || target?.classList.contains("entry-select")) {
      setRowSelected(row, nextSelected);
    } else {
      const wasOnlySelected = selectedPaths.size === 1 && selectedPaths.has(rowPath(row));
      clearSelection();
      setRowSelected(row, !wasOnlySelected);
    }
    lastSelectedIndex = index;
    updateBulkActions();
  }

  function primaryLink(row) {
    return row.querySelector(".col-name a");
  }

  function removeRowsAndFocus(paths, fallback = null) {
    const pathSet = new Set(paths);
    const rowsToRemove = selectableRows.filter(row => pathSet.has(rowPath(row)));
    if (!rowsToRemove.length) return;
    const firstIndex = selectableRows.indexOf(rowsToRemove[0]);
    rowsToRemove.forEach(row => {
      selectedPaths.delete(rowPath(row));
      row.remove();
    });
    lastSelectedIndex = null;
    refreshRows();
    const target = selectableRows[Math.min(firstIndex, selectableRows.length - 1)] || fallback;
    target?.focus?.();
  }

  table?.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    if (!target) return;
    const row = target.closest(".selectable-entry");
    if (!row) return;
    if (target.closest("a, button")) return;
    if (target.classList.contains("entry-select")) {
      event.stopPropagation();
      handleRowSelection(row, event, target.checked);
      return;
    }
    if (target.closest("input")) return;
    handleRowSelection(row, event);
  });

  table?.addEventListener("keydown", event => {
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    const row = target?.closest(".selectable-entry");
    if (!row) {
      if (DELETE_KEYS[event.key] && canDelete && selectedPaths.size && onDeleteSelected) {
        event.preventDefault();
        onDeleteSelected();
      } else if (event.key === "Escape") {
        clearSelection();
      }
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      handleRowSelection(row, event, null, true);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const link = primaryLink(row);
      if (link) onOpen?.(link);
    } else if (DELETE_KEYS[event.key] && canDelete) {
      event.preventDefault();
      if (selectedPaths.size && onDeleteSelected) onDeleteSelected();
      else onDelete?.(row);
    } else if (event.key === "Escape") {
      event.preventDefault();
      clearSelection();
    }
  });

  documentRef.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !selectedPaths.size) return;
    if (documentRef.querySelector(".xwing-dialog-overlay")) return;
    clearSelection();
  }, true);

  selectAll?.addEventListener("change", () => {
    selectableRows.forEach(row => setRowSelected(row, selectAll.checked));
    lastSelectedIndex = null;
    updateBulkActions();
  });

  clearSelectionBtn?.addEventListener("click", clearSelection);

  refreshRows();

  return {
    clearSelection,
    removeRowsAndFocus,
    refreshRows,
    selectedPaths,
    selectedRows: () => selectableRows.filter(row => selectedPaths.has(rowPath(row))),
  };
}
