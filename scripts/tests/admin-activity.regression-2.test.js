import { beforeEach, describe, expect, it, vi } from "vitest";

const selectedPaths = Array.from({ length: 50 }, (_, index) => `/selected-${index + 1}.txt`);
const events = [
  {
    occurred_at: "2026-08-01T12:00:00+00:00",
    username: "admin",
    method: "bulk_delete",
    path: "/_bulk/delete",
    details: JSON.stringify({ count: selectedPaths.length, paths: selectedPaths }),
    status_code: 200,
    duration_ms: 2,
  },
  {
    occurred_at: "2026-08-01T11:59:00+00:00",
    username: "admin",
    method: "bulk_zip",
    path: "/_bulk/zip",
    details: JSON.stringify({ count: selectedPaths.length, paths: selectedPaths }),
    status_code: 200,
    duration_ms: 3,
  },
];

const adminData = {
  "/api/admin/users": { users: [], default: null },
  "/api/admin/metrics": {
    configured_users: 1,
    active_users: 1,
    activity_events: events.length,
    active_window_minutes: 5,
    storage: { files: 2, bytes: 10 },
    trash: { items: 0, bytes: 0 },
  },
  "/api/admin/trash": { transactions: [] },
  "/api/admin/activity": {
    events,
    summary: { event_count: events.length, active_users: 1, by_user: [{ username: "admin", event_count: events.length }] },
  },
};

describe("admin bulk activity details", () => {
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

  it("labels bulk operations and exposes all selected paths", async () => {
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

    await import("../../xwing/frontend/src/admin.ts?admin-activity-details-regression");
    await vi.waitFor(() => expect(document.querySelectorAll(".activity-action")).toHaveLength(2));

    expect([...document.querySelectorAll(".activity-action")].map(element => element.textContent)).toEqual([
      "Moved selected items to trash",
      "Downloaded selected items",
    ]);
    const detailLists = [...document.querySelectorAll(".activity-detail-list")];
    expect(detailLists).toHaveLength(2);
    expect(detailLists.every(details => !details.open)).toBe(true);
    expect(detailLists.map(details => details.querySelector("summary")?.textContent)).toEqual([
      "50 items: /selected-1.txt, /selected-2.txt, /selected-3.txt, +47 more",
      "50 items: /selected-1.txt, /selected-2.txt, /selected-3.txt, +47 more",
    ]);
    expect(detailLists.every(details => details.querySelectorAll("li").length === 50)).toBe(true);
    expect(detailLists[0].textContent).toContain("/selected-50.txt");
  });
});
