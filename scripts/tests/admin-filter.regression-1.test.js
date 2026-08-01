import { beforeEach, describe, expect, it, vi } from "vitest";

const adminData = {
  "/api/admin/users": { users: [], default: null },
  "/api/admin/metrics": {
    configured_users: 1,
    active_users: 1,
    activity_events: 1,
    active_window_minutes: 5,
    storage: { files: 0, bytes: 0 },
    trash: { items: 0, bytes: 0 },
  },
  "/api/admin/trash": { transactions: [] },
  "/api/admin/activity": {
    events: [],
    summary: { event_count: 0, active_users: 0, by_user: [] },
  },
};

describe("admin activity filters", () => {
  beforeEach(() => {
    vi.resetModules();
    window.history.replaceState(null, "", "#activity");
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

  it("preserves username and date filters after refresh", async () => {
    // Regression test for ISSUE-002: .gstack/qa-reports/screenshots/activity-filter-result-beforefix.png
    const fetchMock = vi.fn(async input => {
      const path = new URL(String(input), window.location.origin).pathname;
      return {
        ok: true,
        status: 200,
        url: `${window.location.origin}${path}`,
        json: async () => adminData[path] || adminData["/api/admin/activity"],
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    await import("../../xwing/frontend/src/admin.ts?admin-filter-regression");
    await vi.waitFor(() => expect(document.getElementById("activity-filter")).not.toBeNull());

    const username = document.getElementById("activity-user");
    const since = document.getElementById("activity-since");
    username.value = "admin";
    since.value = "2026-08-01";
    document.getElementById("activity-filter").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("username=admin"),
      expect.objectContaining({ credentials: "same-origin" }),
    ));
    await vi.waitFor(() => {
      expect(document.getElementById("activity-user").value).toBe("admin");
      expect(document.getElementById("activity-since").value).toBe("2026-08-01");
    });
  });
});
