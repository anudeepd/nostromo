import { createAuthSession } from "./shared.js";

declare global {
  interface PromiseConstructor {
    withResolvers<T>(): { promise: Promise<T>; resolve: (value: T | PromiseLike<T>) => void };
  }
}

const authSession = createAuthSession({
  idleTimeoutSeconds: Number(document.body.dataset.authIdleTimeout || "0"),
});
const dialogs = { confirm: confirmAction };

function confirmAction(title: string, message: string, confirmText = "Confirm"): Promise<boolean> {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const backdrop = document.createElement("div");
  const dialog = document.createElement("form");
  const titleElement = document.createElement("h2");
  const messageElement = document.createElement("p");
  const actions = document.createElement("div");
  const cancel = document.createElement("button");
  const confirm = document.createElement("button");
  const { promise, resolve } = Promise.withResolvers<boolean>();
  let closing = false;

  backdrop.className = "modal-backdrop";
  dialog.className = "modal";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "admin-dialog-title");
  dialog.setAttribute("aria-describedby", "admin-dialog-description");
  titleElement.id = "admin-dialog-title";
  titleElement.textContent = title;
  messageElement.id = "admin-dialog-description";
  messageElement.textContent = message;
  actions.className = "modal-actions";
  cancel.className = "button";
  cancel.type = "button";
  cancel.textContent = "Cancel";
  confirm.className = "button danger";
  confirm.type = "submit";
  confirm.textContent = confirmText;
  dialog.append(titleElement, messageElement, actions);
  actions.append(cancel, confirm);
  backdrop.appendChild(dialog);

  const close = (value: boolean): void => {
    if (closing) return;
    closing = true;
    backdrop.classList.add("closing");
    window.setTimeout(() => {
      backdrop.remove();
      if (previous?.isConnected) previous.focus();
      resolve(value);
    }, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 160);
  };
  const focusable = (): HTMLElement[] => [cancel, confirm].filter(button => !button.disabled);
  backdrop.addEventListener("mousedown", event => {
    if (event.target === backdrop) close(false);
  });
  backdrop.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(false);
      return;
    }
    if (event.key !== "Tab") return;
    const controls = focusable();
    if (!controls.length) return;
    const first = controls[0]!;
    const last = controls[controls.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  cancel.addEventListener("click", () => close(false));
  dialog.addEventListener("submit", event => {
    event.preventDefault();
    close(true);
  });
  document.body.appendChild(backdrop);
  window.setTimeout(() => confirm.focus(), 0);
  return promise;
}

function logoMarkup(): string {
  return `<svg class="brand-mark" viewBox="0 0 200 200" aria-label="X-wing logo">
    <rect x="6" y="6" width="188" height="188" rx="36"></rect>
    <g fill="none" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="71,78 23,48 15,100 23,152 71,122"></polygon><polyline points="71,78 30,100 71,122"></polyline>
      <polygon points="129,78 177,48 185,100 177,152 129,122"></polygon><polyline points="129,78 170,100 129,122"></polyline>
      <path d="m71 78 15 8m-15 36 15-8m43-36-15 8m15 36-15-8"></path>
      <circle cx="100" cy="100" r="20"></circle><circle cx="100" cy="100" r="13"></circle>
    </g><circle class="brand-core" cx="100" cy="100" r="4.5"></circle>
  </svg>`;
}
type PermissionSet = { read: boolean; write: boolean; delete: boolean };
type UserRecord = { username: string; permissions: PermissionSet };
type Metrics = {
  configured_users: number;
  active_users: number;
  activity_events: number;
  active_window_minutes: number;
  storage: { files: number; bytes: number };
  trash: { items: number; bytes: number };
};
type ActivityEvent = {
  occurred_at: string;
  username: string;
  method: string;
  path: string;
  details: string | null;
  status_code: number;
  duration_ms: number;
};
type TrashTransaction = {
  transaction_id: string;
  user: string;
  created: string;
  size: number;
  items: { path: string; kind: string; size: number }[];
};
type AdminState = {
  users: UserRecord[];
  defaultPermissions: PermissionSet | null;
  metrics: Metrics | null;
  events: ActivityEvent[];
  activitySummary: { event_count: number; active_users: number; by_user: { username: string; event_count: number }[] };
  trash: TrashTransaction[];
};

const bootstrap = JSON.parse(document.getElementById("admin-bootstrap")?.textContent || "{}") as { user: string; ldapConfigured: boolean };
const state: AdminState = {
  users: [], defaultPermissions: null, metrics: null, events: [],
  activitySummary: { event_count: 0, active_users: 0, by_user: [] }, trash: [],
};
const tabNames = ["overview", "users", "activity", "trash"];
const requestedTab = location.hash.slice(1);
let activeTab = tabNames.includes(requestedTab) ? requestedTab : "overview";
let accountOpen = false;
let accountOutsideHandler: ((event: PointerEvent) => void) | null = null;
let suppressViewAnimation = false;

function clearAccountOutsideHandler(): void {
  if (accountOutsideHandler) document.removeEventListener("pointerdown", accountOutsideHandler);
  accountOutsideHandler = null;
}

function closeAccountMenu(): void {
  accountOpen = false;
  clearAccountOutsideHandler();
  suppressViewAnimation = true;
  render();
}

function accountChevronMarkup(): string {
  return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"></path></svg>`;
}

function accountMarkup(): string {
  return `<div class="account" id="account-control">
    <button class="account-trigger" type="button" aria-haspopup="menu" aria-expanded="${accountOpen}"><span>${escapeHtml(bootstrap.user)}</span>${accountChevronMarkup()}</button>
    ${accountOpen ? `<div class="popover account-menu" role="menu" aria-label="Workspace navigation">
      <a class="menu-item" href="/" role="menuitem"><span class="workspace-nav-dot" aria-hidden="true"></span>Files</a>
      <a class="menu-item active" href="/admin" role="menuitem" aria-current="page"><span class="workspace-nav-dot" aria-hidden="true"></span>Admin panel</a>
    </div>` : ""}
  </div>`;
}
const root = document.getElementById("admin-root") as HTMLElement;

function escapeHtml(value: unknown): string {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit ? value.toFixed(1) : value.toFixed(0)} ${units[unit] ?? "B"}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await authSession.authFetch(url, { credentials: "same-origin", ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { const body = await response.json() as { detail?: string }; message = body.detail || message; } catch { /* use status */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function permissionsMarkup(permissions: PermissionSet, prefix: string, disabled = false): string {
  return `<div class="permission-grid" role="group" aria-label="Permissions">
    ${(["read", "write", "delete"] as const).map(permission => `<label class="check-label"><input type="checkbox" name="${prefix}-${permission}" ${permissions[permission] ? "checked" : ""} ${disabled ? "disabled" : ""}/> ${permission.charAt(0).toUpperCase()}${permission.slice(1)}</label>`).join("")}
  </div>`;
}

function readPermissions(form: HTMLFormElement, prefix: string): PermissionSet {
  const value = (permission: keyof PermissionSet): boolean => (form.elements.namedItem(`${prefix}-${permission}`) as HTMLInputElement).checked;
  return { read: value("read"), write: value("write"), delete: value("delete") };
}

function showError(error: unknown): void {
  const message = error instanceof Error ? error.message : "Something went wrong";
  const target = document.getElementById("admin-feedback");
  if (target) { target.textContent = message; target.className = "admin-feedback error"; }
}

function showSuccess(message: string): void {
  const target = document.getElementById("admin-feedback");
  if (target) { target.textContent = message; target.className = "admin-feedback success"; }
}

function metricCard(label: string, value: string, hint: string): string {
  return `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(hint)}</small></article>`;
}

function loadActiveTab(): void {
  if (activeTab === "overview") void loadMetrics();
  if (activeTab === "users") void loadUsers();
  if (activeTab === "activity") void loadActivity();
  if (activeTab === "trash") void loadTrash();
}


function render(): void {
  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "users", label: "Users" },
    { id: "activity", label: "Activity" },
    { id: "trash", label: "Trash" },
  ];
  root.innerHTML = `<div class="admin-shell">
    <header class="topbar admin-topbar"><div class="brand">${logoMarkup()}<span>X-wing</span><small class="brand-context">ADMIN</small></div><div class="account-inline">${accountMarkup()}<form id="logout-form" method="post" action="/_auth/logout"><button class="signout-button" type="submit">Sign out</button></form></div></header>
    <main id="admin-main" class="admin-main"><div class="admin-heading"><div><p class="eyebrow">CONTROL PLANE</p><h1>Workspace administration</h1><p class="lede">Manage user access, activity, and recoverable storage.</p></div><div id="admin-feedback" class="admin-feedback" role="status" aria-live="polite"></div></div>
      <nav class="admin-tabs" aria-label="Admin sections">${tabs.map(tab => `<a class="admin-tab ${activeTab === tab.id ? "active" : ""}" data-admin-tab="${tab.id}" href="#${tab.id}">${tab.label}</a>`).join("")}</nav>
      <section id="admin-view" class="admin-view ${suppressViewAnimation ? "no-motion" : ""}" aria-live="polite">${viewMarkup()}</section>
    </main>
  </div>`;
  suppressViewAnimation = false;
  root.querySelectorAll<HTMLElement>("[data-admin-tab]").forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    const nextTab = link.dataset.adminTab || "overview";
    if (nextTab === activeTab) return;
    activeTab = nextTab;
    history.pushState(null, "", `#${activeTab}`);
    render();
    loadActiveTab();
  }));
  bindView();
  bindAccountMenu();
  authSession.wireLogoutForm();
}

window.addEventListener("popstate", () => {
  const nextTab = location.hash.slice(1);
  if (!tabNames.includes(nextTab) || nextTab === activeTab) return;
  activeTab = nextTab;
  render();
  loadActiveTab();
});

function bindAccountMenu(): void {
  const control = document.getElementById("account-control");
  const trigger = control?.querySelector<HTMLButtonElement>(".account-trigger");
  trigger?.addEventListener("click", () => {
    accountOpen = !accountOpen;
    if (!accountOpen) clearAccountOutsideHandler();
    suppressViewAnimation = true;
    render();
  });
  if (!accountOpen || !control) return;
  accountOutsideHandler = event => {
    if (!control.contains(event.target as Node)) closeAccountMenu();
  };
  document.addEventListener("pointerdown", accountOutsideHandler);
}

function viewMarkup(): string {
  if (activeTab === "users") return usersMarkup();
  if (activeTab === "activity") return activityMarkup();
  if (activeTab === "trash") return trashMarkup();
  return overviewMarkup();
}

function overviewMarkup(): string {
  const metrics = state.metrics;
  if (!metrics) return `<div class="admin-card loading-card" role="status">Loading admin data…</div>`;
  return `<div class="metric-grid">
    ${metricCard("Configured users", String(metrics.configured_users), "Explicit entries in users.yaml")}
    ${metricCard("Active users", String(metrics.active_users), `Seen in audit log, last ${metrics.active_window_minutes} minutes`)}
    ${metricCard("Activity", String(metrics.activity_events), `Recorded events, last ${metrics.active_window_minutes} minutes`)}
    ${metricCard("Stored files", String(metrics.storage.files), formatBytes(metrics.storage.bytes))}
    ${metricCard("Recoverable trash", String(metrics.trash.items), formatBytes(metrics.trash.bytes))}
  </div>`;
}

function userAccessHelp(): string {
  return bootstrap.ldapConfigured
    ? "LDAPGate users are synchronized to ldap.allowed_users; restart X-wing after access changes."
    : "Permissions apply to authenticated usernames listed in users.yaml.";
}

function usersMarkup(): string {
  const emptyPermissions: PermissionSet = { read: true, write: false, delete: false };
  return `<div class="section-grid"><article class="admin-card"><div class="card-heading"><div><p class="eyebrow">DIRECTORY</p><h2>Users</h2></div><span class="count-badge">${state.users.length}</span></div><div class="table-wrap"><table class="user-table"><thead><tr><th scope="col">Username</th><th scope="col">Permissions</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${state.users.map(user => `<tr><td><strong>${escapeHtml(user.username)}</strong></td><td>${permissionBadges(user.permissions)}</td><td><div class="row-actions"><button class="button small" data-edit-user="${escapeHtml(user.username)}" aria-label="Edit user ${escapeHtml(user.username)}">Edit</button><button class="button small danger" data-delete-user="${escapeHtml(user.username)}" aria-label="Remove user ${escapeHtml(user.username)}">Remove</button></div></td></tr>`).join("") || `<tr><td colspan="3" class="empty-cell">No explicit users configured.</td></tr>`}</tbody></table></div>${state.defaultPermissions ? `<div class="default-access"><strong>Wildcard default</strong>${permissionBadges(state.defaultPermissions)}<span>Applies to users not listed above.</span></div>` : ""}</article><article class="admin-card form-card"><p class="eyebrow">USER ENTRY</p><h2 id="user-form-title">Add or update user</h2><form id="user-form"><input type="hidden" name="original-username"/><label for="username">Username</label><input id="username" name="username" required maxlength="128" autocomplete="off"/><p class="field-help">${escapeHtml(userAccessHelp())}</p>${permissionsMarkup(emptyPermissions, "user")}<div class="form-actions"><button class="button primary" type="submit">Save user</button><button class="button" type="button" id="clear-user">Clear</button></div></form></article></div>`;
}

function permissionBadges(permissions: PermissionSet): string {
  return `<span class="permission-badges">${(["read", "write", "delete"] as const).filter(permission => permissions[permission]).map(permission => `<span class="permission-badge ${permission}">${permission}</span>`).join("") || `<span class="permission-badge none">none</span>`}</span>`;
}


const activityLabels: Record<string, string> = {
  upload: "Uploaded",
  download: "Downloaded",
  delete: "Moved to trash",
  bulk_delete: "Moved to trash",
  restore: "Restored",
  mkdir: "Created folder",
  copy: "Copied",
  move: "Moved",
  admin_user_upsert: "Saved user",
  admin_user_delete: "Removed user",
  admin_ldap_update: "Updated LDAPGate config",
  admin_trash_restore: "Restored from trash",
  admin_trash_delete: "Deleted from trash",
  admin_audit_purge: "Purged audit history",
};

function activityLabel(method: string): string {
  return activityLabels[method] || method.replace(/^admin_/, "").replace(/_/g, " ");
}

function activityDetails(event: ActivityEvent): string | null {
  if (!event.details) return null;
  try {
    const value: unknown = JSON.parse(event.details);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const details = value as Record<string, unknown>;
    const count = Number(details.count ?? details.restored ?? details.deleted);
    if (Number.isFinite(count) && count > 0) return `${count} item${count === 1 ? "" : "s"}`;
    if (event.method === "upload" && Number.isFinite(Number(details.bytes))) return `${formatBytes(Number(details.bytes))}`;
    if (event.method === "admin_user_upsert" || event.method === "admin_user_delete") {
      return typeof details.username === "string" ? details.username : null;
    }
    if (event.method === "admin_audit_purge") {
      const days = Number(details.older_than_days);
      return Number.isFinite(days) ? `Older than ${days} days` : null;
    }
    return null;
  } catch {
    return null;
  }
}


function activityRow(event: ActivityEvent): string {
  const detail = activityDetails(event);
  return `<tr><td>${escapeHtml(formatDate(event.occurred_at))}</td><td><strong>${escapeHtml(event.username)}</strong></td><td><span class="activity-action">${escapeHtml(activityLabel(event.method))}</span>${detail ? `<small class="activity-detail">${escapeHtml(detail)}</small>` : ""}</td><td class="path-cell" title="${escapeHtml(event.path)}"><code>${escapeHtml(event.path)}</code></td><td><span class="status-code ${event.status_code >= 400 ? "bad" : "good"}">${event.status_code}</span></td></tr>`;
}


function activityMarkup(): string {
  return `<article class="admin-card"><div class="card-heading"><div><p class="eyebrow">AUDIT TRAIL</p><h2>User activity</h2></div><span class="count-badge">${state.activitySummary.event_count}</span></div><div class="activity-tools"><form id="activity-filter" class="inline-form activity-filter-form"><div class="filter-field"><label for="activity-user">User</label><input id="activity-user" name="username" placeholder="All users"/></div><div class="filter-field"><label for="activity-since">Since</label><input id="activity-since" name="since" type="date"/></div><div class="filter-field"><label for="activity-scope">Show</label><select id="activity-scope" name="scope"><option value="file" selected>File activity</option><option value="all">All events</option><option value="admin">Administration</option></select></div><button class="button" type="submit">Refresh</button></form><form id="audit-purge-form" class="inline-form purge-form"><div class="filter-field"><label for="audit-retention">Purge older than</label><div class="input-with-unit"><input id="audit-retention" name="older_than_days" type="number" min="1" max="36500" value="90" required/><span class="unit-label">days</span></div></div><button class="button danger" type="submit">Purge history</button></form></div><div class="table-wrap activity-table"><table><thead><tr><th scope="col">Time</th><th scope="col">User</th><th scope="col">Action</th><th scope="col">Path</th><th scope="col">Status</th></tr></thead><tbody>${state.events.map(activityRow).join("") || `<tr><td colspan="5" class="empty-cell">No activity matches filter.</td></tr>`}</tbody></table></div></article>`;
}

function trashMarkup(): string {
  return `<article class="admin-card"><div class="card-heading"><div><p class="eyebrow">RECOVERY</p><h2>Recoverable trash</h2></div><button class="button" id="refresh-trash">Refresh</button></div><p class="field-help trash-help">Deleted items stay here until restored or permanently removed.</p><div class="table-wrap"><table class="trash-table"><thead><tr><th scope="col">Deleted by</th><th scope="col">Items</th><th scope="col">Deleted</th><th scope="col">Size</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${state.trash.map(transaction => `<tr><td><strong>${escapeHtml(transaction.user)}</strong></td><td class="trash-items">${transaction.items.map(item => `<div class="trash-path"><span class="trash-kind">${escapeHtml(item.kind)}</span><code>${escapeHtml(item.path)}</code></div>`).join("")}</td><td>${escapeHtml(formatDate(transaction.created))}</td><td>${escapeHtml(formatBytes(transaction.size))}</td><td><div class="row-actions"><button class="button small" data-restore-trash="${escapeHtml(transaction.transaction_id)}" aria-label="Restore deleted items">Restore</button><button class="button small danger" data-delete-trash="${escapeHtml(transaction.transaction_id)}" aria-label="Permanently delete items">Delete permanently</button></div></td></tr>`).join("") || `<tr><td colspan="5" class="empty-cell">Trash is empty.</td></tr>`}</tbody></table></div></article>`;
}

function bindView(): void {
  const userForm = document.getElementById("user-form") as HTMLFormElement | null;
  userForm?.addEventListener("submit", event => { event.preventDefault(); void saveUser(userForm); });
  document.getElementById("clear-user")?.addEventListener("click", () => { userForm?.reset(); const title = document.getElementById("user-form-title"); if (title) title.textContent = "Add or update user"; });
  root.querySelectorAll<HTMLButtonElement>("[data-edit-user]").forEach(button => button.addEventListener("click", () => editUser(button.dataset.editUser || "")));
  root.querySelectorAll<HTMLButtonElement>("[data-delete-user]").forEach(button => button.addEventListener("click", () => {
    const username = button.dataset.deleteUser || "";
    void dialogs.confirm(`Remove ${username}?`, "This removes users.yaml permissions and LDAP access.", "Remove user").then(confirmed => {
      if (confirmed) void deleteUser(username);
    });
  }));
  const activityForm = document.getElementById("activity-filter") as HTMLFormElement | null;
  activityForm?.addEventListener("submit", event => { event.preventDefault(); void loadActivity(activityForm); });
  const purgeForm = document.getElementById("audit-purge-form") as HTMLFormElement | null;
  purgeForm?.addEventListener("submit", event => {
    event.preventDefault();
    void dialogs.confirm("Purge audit history?", "Matching audit events will be permanently deleted.", "Purge history").then(confirmed => {
      if (confirmed) void purgeAuditHistory(purgeForm);
    });
  });
  document.getElementById("refresh-trash")?.addEventListener("click", () => void loadTrash());
  root.querySelectorAll<HTMLButtonElement>("[data-restore-trash]").forEach(button => button.addEventListener("click", () => void restoreTrash(button.dataset.restoreTrash || "")));
  root.querySelectorAll<HTMLButtonElement>("[data-delete-trash]").forEach(button => button.addEventListener("click", () => {
    const transactionId = button.dataset.deleteTrash || "";
    void dialogs.confirm("Delete trash permanently?", "Selected deleted items cannot be restored after this action.", "Delete permanently").then(confirmed => {
      if (confirmed) void deleteTrash(transactionId);
    });
  }));
}

function editUser(username: string): void {
  const user = state.users.find(item => item.username === username);
  const form = document.getElementById("user-form") as HTMLFormElement | null;
  if (!user || !form) return;
  (form.elements.namedItem("username") as HTMLInputElement).value = user.username;
  (form.elements.namedItem("original-username") as HTMLInputElement).value = user.username;
  for (const permission of ["read", "write", "delete"] as const) (form.elements.namedItem(`user-${permission}`) as HTMLInputElement).checked = user.permissions[permission];
  const title = document.getElementById("user-form-title"); if (title) title.textContent = `Edit ${username}`;
  (form.elements.namedItem("username") as HTMLInputElement).focus();
}

async function saveUser(form: HTMLFormElement): Promise<void> {
  try {
    const username = (form.elements.namedItem("username") as HTMLInputElement).value;
    const originalUsername = (form.elements.namedItem("original-username") as HTMLInputElement).value || null;
    const result = await api<{ restart_required: boolean }>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({
        username,
        original_username: originalUsername,
        permissions: readPermissions(form, "user"),
      }),
    });
    await loadUsers();
    showSuccess(
      result.restart_required
        ? `Saved ${username}. Restart X-wing to apply LDAP access changes.`
        : `Saved ${username}.`,
    );
  } catch (error) { showError(error); }
}

async function deleteUser(username: string): Promise<void> {
  try {
    const result = await api<{ restart_required: boolean }>(
      `/api/admin/users/${encodeURIComponent(username)}`,
      { method: "DELETE" },
    );
    await loadUsers();
    showSuccess(
      result.restart_required
        ? `Deleted ${username}. Restart X-wing to apply LDAP access changes.`
        : `Deleted ${username}.`,
    );
  } catch (error) { showError(error); }
}

async function loadUsers(): Promise<void> { try { const result = await api<{ users: UserRecord[]; default: PermissionSet | null }>("/api/admin/users"); state.users = result.users; state.defaultPermissions = result.default; suppressViewAnimation = true; render(); } catch (error) { showError(error); } }
async function loadTrash(): Promise<void> { try { const result = await api<{ transactions: TrashTransaction[] }>("/api/admin/trash"); state.trash = result.transactions; suppressViewAnimation = true; render(); } catch (error) { showError(error); } }
async function loadMetrics(): Promise<void> { try { state.metrics = await api<Metrics>("/api/admin/metrics"); suppressViewAnimation = true; render(); } catch (error) { showError(error); } }
async function loadActivity(form?: HTMLFormElement): Promise<void> { try { const params = new URLSearchParams({ limit: "200" }); const username = form && (form.elements.namedItem("username") as HTMLInputElement).value; const since = form && (form.elements.namedItem("since") as HTMLInputElement).value; const scope = form ? (form.elements.namedItem("scope") as HTMLSelectElement).value : "file"; if (username) params.set("username", username); if (since) params.set("since", `${since}T00:00:00+00:00`); params.set("scope", scope); const result = await api<{ events: ActivityEvent[]; summary: AdminState["activitySummary"] }>(`/api/admin/activity?${params}`); state.events = result.events; state.activitySummary = result.summary; suppressViewAnimation = true; render(); const nextUsername = document.getElementById("activity-user") as HTMLInputElement | null; if (nextUsername) nextUsername.value = username || ""; const nextSince = document.getElementById("activity-since") as HTMLInputElement | null; if (nextSince) nextSince.value = since || ""; const nextScope = document.getElementById("activity-scope") as HTMLSelectElement | null; if (nextScope) nextScope.value = scope; } catch (error) { showError(error); } }
async function purgeAuditHistory(form: HTMLFormElement): Promise<void> { try { const days = (form.elements.namedItem("older_than_days") as HTMLInputElement).value; const result = await api<{ deleted: number; older_than_days: number }>(`/api/admin/activity?older_than_days=${encodeURIComponent(days)}`, { method: "DELETE" }); await loadActivity(); showSuccess(`Purged ${result.deleted} audit event${result.deleted === 1 ? "" : "s"}.`); } catch (error) { showError(error); } }

async function restoreTrash(transactionId: string): Promise<void> { try { const result = await api<{ restored: number }>(`/api/admin/trash/${encodeURIComponent(transactionId)}/restore`, { method: "POST" }); await loadTrash(); showSuccess(`${result.restored} item${result.restored === 1 ? "" : "s"} restored.`); } catch (error) { showError(error); } }
async function deleteTrash(transactionId: string): Promise<void> { try { const result = await api<{ deleted: number }>(`/api/admin/trash/${encodeURIComponent(transactionId)}`, { method: "DELETE" }); await loadTrash(); showSuccess(`${result.deleted} trash item${result.deleted === 1 ? "" : "s"} permanently deleted.`); } catch (error) { showError(error); } }

async function loadData(): Promise<void> {
  try {
    const [users, metrics, trash, activity] = await Promise.all([
      api<{ users: UserRecord[]; default: PermissionSet | null }>("/api/admin/users"),
      api<Metrics>("/api/admin/metrics"),
      api<{ transactions: TrashTransaction[] }>("/api/admin/trash"),
      api<{ events: ActivityEvent[]; summary: AdminState["activitySummary"] }>("/api/admin/activity?limit=200"),
    ]);
    state.users = users.users; state.defaultPermissions = users.default; state.metrics = metrics; state.trash = trash.transactions; state.events = activity.events; state.activitySummary = activity.summary;
    render();
  } catch (error) { root.innerHTML = `<div class="admin-fatal" role="alert"><strong>Admin console unavailable</strong><span>${escapeHtml(error instanceof Error ? error.message : "Request failed")}</span><a class="button" href="/">Return to files</a></div>`; }
}

authSession.wireAuthIdleTimer();
void loadData();
