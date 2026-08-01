import { beforeEach, describe, expect, it, vi } from "vitest";

const transactions = [
  { transaction_id: "tx-third", user: "admin", created: "2026-08-01T12:02:00+00:00", size: 10, items: [{ path: "/third.txt", kind: "file", size: 10 }] },
  { transaction_id: "tx-second", user: "admin", created: "2026-08-01T12:01:00+00:00", size: 11, items: [{ path: "/second.txt", kind: "file", size: 11 }] },
  { transaction_id: "tx-first", user: "admin", created: "2026-08-01T12:00:00+00:00", size: 10, items: [{ path: "/first.txt", kind: "file", size: 10 }] },
];

const adminData = {
  "/api/admin/users": { users: [], default: null },
  "/api/admin/metrics": {
    configured_users: 1,
    active_users: 1,
    activity_events: 0,
    active_window_minutes: 5,
    storage: { files: 0, bytes: 0 },
    trash: { items: 3, bytes: 31 },
  },
  "/api/admin/trash": { transactions },
  "/api/admin/activity": {
    events: [],
    summary: { event_count: 0, active_users: 0, by_user: [] },
  },
};

describe("admin trash selection labels", () => {
  beforeEach(() => {
    vi.resetModules();
    window.history.replaceState(null, "", "#trash");
    document.body.dataset.authIdleTimeout = "0";
    document.body.innerHTML = `
      <div id="admin-root"></div>
      <div id="auth-overlay" hidden>
        <h2 id="auth-overlay-title"></h2>
        <p id="auth-overlay-message"></p>
      </div>
      <script id="admin-bootstrap" type="application/json">{"user":"admin","ldapConfigured":false}</script>
    `;
  });

  it("identifies each trash selection by deleted path", async () => {
    // Regression: ISSUE-001 — trash selections used duplicate accessible labels.
    // Found by /qa on 2026-08-01
    // Report: .gstack/qa-reports/qa-report-localhost-2026-08-01.md
    const fetchMock = vi.fn(async input => {
      const path = new URL(String(input), window.location.origin).pathname;
      return {
        ok: true,
        status: 200,
        url: `${window.location.origin}${path}`,
        json: async () => adminData[path],
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    await import("../../xwing/frontend/src/admin.ts?admin-trash-labels-regression");
    await vi.waitFor(() => expect(document.querySelectorAll("[data-select-trash]")).toHaveLength(3));

    expect([...document.querySelectorAll("[data-select-trash]")].map(input => input.getAttribute("aria-label"))).toEqual([
      "Select trash transaction for /third.txt",
      "Select trash transaction for /second.txt",
      "Select trash transaction for /first.txt",
    ]);
  });
});
