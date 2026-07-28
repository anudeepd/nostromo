import { expect, test } from "@playwright/test";

test("daily browser workflow is keyboard-accessible", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("img", { name: "X-wing logo" })).toBeVisible();
  await expect(page.getByText("X-wing", { exact: true })).toBeVisible();
  await expect(page.getByText("workspace", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Parallel uploads: 4" }).click();
  const menu = page.getByRole("dialog", { name: "Concurrent uploads" });
  await expect(menu).toBeVisible();
  await menu.getByRole("radio", { name: "8" }).click();
  await expect(page.getByRole("button", { name: "Parallel uploads: 8" })).toBeVisible();

  const readme = page.getByRole("row", { name: /^README\.md,/ });
  await page.getByRole("checkbox", { name: "Select README.md" }).click();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await expect.poll(() => readme.evaluate(element => document.activeElement === element)).toBe(true);
  const releases = page.getByRole("row", { name: /^releases,/ });
  await releases.click({ modifiers: ["Shift"] });
  await expect(page.getByText("2 selected", { exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString() ?? "")).toBe("");
  await releases.click();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await releases.press("ArrowDown");
  await page.keyboard.press("Space");
  await expect(page.getByText("2 selected", { exact: true })).toBeVisible();
  await page.keyboard.press("Delete");
  await expect(page.getByRole("dialog")).toContainText("Delete 2 items?");
  await page.getByRole("button", { name: "Cancel" }).click();
  await releases.focus();
  await releases.press("Escape");
  await expect(page.getByText("2 selected", { exact: true })).not.toBeVisible();
  await releases.focus();
  await releases.press("Enter");
  await expect(page).toHaveURL(/\/releases\/$/);
  await expect(page.getByRole("row", { name: /^checksums\.txt,/ })).toBeFocused();
  await page.goBack();
  await expect(page.getByRole("row", { name: /^README\.md,/ })).toBeVisible();
});

test("file keyboard commands respect focus ownership", async ({ page }) => {
  await page.goto("/");

  const releases = page.getByRole("row", { name: /^releases,/ });
  const releaseCheckbox = page.getByRole("checkbox", { name: "Select releases" });
  const releaseDelete = page.getByRole("button", { name: "Delete releases" });

  await releases.focus();
  await page.keyboard.press("Tab");
  await expect(releaseCheckbox).toBeFocused();
  await page.keyboard.press("Space");
  await expect(releaseCheckbox).toBeChecked();
  await expect(releaseCheckbox).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(releaseCheckbox).not.toBeChecked();

  await releaseDelete.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Delete 1 item?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Delete" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Delete" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(releaseDelete).toBeFocused();
  await expect(page).toHaveURL(/\/$/);
});

test("row shortcuts and global selection escape work from natural focus", async ({ page }) => {
  await page.goto("/");

  const releases = page.getByRole("row", { name: /^releases,/ });
  await releases.focus();
  await page.keyboard.press("Space");
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "New folder" }).focus();
  await page.keyboard.press("Escape");
  await expect(page.getByText("1 selected", { exact: true })).not.toBeVisible();

  await releases.focus();
  await page.keyboard.press("Delete");
  const dialog = page.getByRole("dialog", { name: "Delete 1 item?" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(releases).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/releases\/$/);
});

test("Delete works again after dismissing a mouse-opened delete dialog", async ({ page }) => {
  await page.goto("/");

  const releaseCheckbox = page.getByRole("checkbox", { name: "Select releases" });
  await releaseCheckbox.click();
  await expect(releaseCheckbox).toBeChecked();

  const rowDelete = page.getByRole("button", { name: "Delete releases" });
  await rowDelete.click();
  const dialog = page.getByRole("dialog", { name: "Delete 1 item?" });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(releaseCheckbox).toBeChecked();
  await expect(rowDelete).toBeFocused();

  await page.keyboard.press("Delete");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");

  const bulkDelete = page.getByRole("button", { name: "Delete selected" });
  await bulkDelete.click();
  await page.keyboard.press("Escape");
  await expect(bulkDelete).toBeFocused();
  await expect(releaseCheckbox).toBeChecked();

  await page.keyboard.press("Delete");
  await expect(dialog).toBeVisible();
});

test("selection controls preserve a useful keyboard focus", async ({ page }) => {
  await page.goto("/");

  const releases = page.getByRole("row", { name: /^releases,/ });
  await releases.click();
  await page.getByRole("button", { name: "Clear" }).click();

  await expect(releases).toBeFocused();
  await expect(page.getByText("1 selected", { exact: true })).not.toBeVisible();
});

test("mouse and keyboard transitions keep layer and trigger ownership", async ({ page }) => {
  await page.goto("/");

  const newFolder = page.getByRole("button", { name: "New folder" });
  await newFolder.click();
  const folderDialog = page.getByRole("dialog", { name: "New folder" });
  await expect(folderDialog.getByRole("textbox", { name: "Folder name" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator(".modal-backdrop")).toHaveClass(/closing/);
  await expect(newFolder).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(folderDialog).toBeVisible();
  await page.keyboard.press("Escape");

  const parallel = page.getByRole("button", { name: /Parallel uploads:/ });
  await parallel.click();
  const parallelDialog = page.getByRole("dialog", { name: "Concurrent uploads" });
  await page.keyboard.press("Escape");
  await expect(parallelDialog).not.toBeVisible();
  await expect(parallel).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(parallelDialog).toBeVisible();
  await page.keyboard.press("Escape");

  const anonymous = page.getByText("anonymous", { exact: true });
  await expect(anonymous).toHaveCSS("font-size", "12px");
  await page.getByRole("row", { name: /^releases,/ }).click();
  await expect(anonymous).toBeVisible();
  await expect(page.getByRole("button", { name: "Account: anonymous" })).toHaveCount(0);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByText("1 selected", { exact: true })).not.toBeVisible();
});

test("dragging files exposes feedback and a delayed-drop fallback", async ({ page }) => {
  await page.clock.install();
  await page.goto("/");

  const app = page.locator(".xw-app");
  await app.dispatchEvent("dragenter");
  await app.dispatchEvent("dragover");
  const target = page.getByRole("status");
  await expect(target).toContainText("Drop files here");
  await expect(target).toContainText("Upload to /");

  await page.clock.fastForward(1000);
  await app.dispatchEvent("dragover");
  await page.clock.fastForward(1000);
  await expect(target).toContainText("Drop files here");

  await page.clock.fastForward(500);
  await expect(page.getByRole("status")).toHaveText("Preparing upload…");

  await page.clock.fastForward(15000);
  await expect(page.getByRole("status")).toHaveText("Upload hasn't started yet.");
  await expect(page.getByRole("button", { name: "Choose files" })).toBeVisible();

  await page.getByRole("button", { name: "Dismiss upload status" }).click();
  await expect(page.getByText("Upload hasn't started yet.")).not.toBeVisible();
});

test("a completed browser upload refreshes the folder automatically", async ({ page }) => {
  await page.goto("/");

  const bootstrap = JSON.parse(await page.locator("#xwing-bootstrap").textContent() ?? "{}") as { files: unknown[] } & Record<string, unknown>;
  let directoryRefreshes = 0;
  await page.route("**/*", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/_upload/")) {
      if (path === "/_upload/init") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ session_id: "browser-upload" }) });
      }
      if (path.endsWith("/complete")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ path: "/browser-upload.txt" }) });
      }
      return route.fulfill({ status: 204 });
    }
    const accept = route.request().headers().accept ?? "";
    if (route.request().method() !== "GET" || !accept.includes("application/vnd.xwing.directory+json")) {
      return route.continue();
    }
    directoryRefreshes += 1;
    await new Promise(resolve => setTimeout(resolve, 120));
    return route.fulfill({
      status: 200,
      contentType: "application/vnd.xwing.directory+json",
      body: JSON.stringify({
        ...bootstrap,
        files: [...bootstrap.files, {
          name: "browser-upload.txt",
          path: "/browser-upload.txt",
          kind: "file",
          size: 18,
          modified: "2026-07-19T12:00:00Z",
          editable: true,
        }],
      }),
    });
  });

  await page.locator("input[type=file]").first().setInputFiles({
    name: "browser-upload.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("real browser fetch"),
  });

  const upload = page.getByRole("complementary", { name: "Uploads" });
  await expect(upload).toContainText("1 complete");
  await expect(upload).toContainText("Upload complete");
  await expect(page.getByRole("row", { name: /^browser-upload\.txt,/ })).toBeVisible();
  await expect.poll(() => directoryRefreshes).toBe(1);
  await expect(page.getByRole("button", { name: "Refresh folder" })).toHaveCount(0);
  await expect(upload).not.toBeVisible({ timeout: 6000 });
});

test("nested file controls preserve native keys and file commands", async ({ page }) => {
  await page.goto("/");

  const releases = page.getByRole("row", { name: /^releases,/ });
  const releaseCheckbox = page.getByRole("checkbox", { name: "Select releases" });
  const readmeCheckbox = page.getByRole("checkbox", { name: "Select README.md" });
  await releaseCheckbox.click();
  await readmeCheckbox.click({ modifiers: ["Shift"] });
  await expect(page.getByText("2 selected", { exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: /^README\.md,/ })).toBeFocused();
  await expect.poll(() => page.evaluate(() => window.getSelection()?.toString() ?? "")).toBe("");

  const download = page.getByRole("link", { name: "Download releases" });
  await download.focus();
  await page.keyboard.press("Delete");
  const dialog = page.getByRole("dialog", { name: "Delete 2 items?" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(download).toBeFocused();

  await page.getByRole("button", { name: /Name/ }).focus();
  await page.keyboard.press("Delete");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await releases.focus();
  await page.keyboard.press("Escape");
});

test("successful deletion focuses the nearest surviving row", async ({ page }) => {
  await page.goto("/");
  const bootstrap = JSON.parse(await page.locator("#xwing-bootstrap").textContent() ?? "{}") as { files: Array<{ path: string }> };
  let deleted = false;
  await page.route("**/*", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "DELETE" && path === "/releases/") {
      deleted = true;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ transaction_id: "delete-1" }) });
    } else if (request.method() === "POST" && path === "/api/restore/delete-1") {
      deleted = false;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ restored: 1 }) });
    } else if (deleted && request.method() === "GET" && path === "/" && request.headers().accept?.includes("application/vnd.xwing.directory+json")) {
      await route.fulfill({ status: 200, contentType: "application/vnd.xwing.directory+json", body: JSON.stringify({ ...bootstrap, files: bootstrap.files.filter(file => file.path !== "/releases/") }) });
    } else await route.continue();
  });

  const releases = page.getByRole("row", { name: /^releases,/ });
  await releases.focus();
  await page.keyboard.press("Delete");
  await page.getByRole("dialog", { name: "Delete 1 item?" }).getByRole("button", { name: "Delete" }).click();

  await expect(releases).not.toBeVisible();
  await expect(page.getByRole("row", { name: /^README\.md,/ })).toBeFocused();
  const deletedToast = page.getByRole("status").filter({ hasText: "1 item deleted" });
  await expect(deletedToast).toHaveClass(/deleted/);
  await expect(deletedToast.locator(".toast-timer")).toHaveCSS("animation-duration", "15s");
  await deletedToast.getByRole("button", { name: "Undo" }).click();
  await expect(deletedToast).not.toBeVisible();
  const restoredToast = page.getByRole("status").filter({ hasText: "1 item restored" });
  await expect(restoredToast).toHaveClass(/restored/);
  await expect(restoredToast.locator(".toast-timer")).toHaveCSS("animation-duration", "15s");
  await expect(page.getByRole("row", { name: /^releases,/ })).toBeVisible();
});

test("failed deletion keeps the dialog keyboard-operable", async ({ page }) => {
  await page.route("**/releases/", route => route.request().method() === "DELETE"
    ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Storage unavailable" }) })
    : route.continue());
  await page.goto("/");

  await page.getByRole("row", { name: /^releases,/ }).focus();
  await page.keyboard.press("Delete");
  const dialog = page.getByRole("dialog", { name: "Delete 1 item?" });
  const confirm = dialog.getByRole("button", { name: "Delete" });
  await confirm.click();

  await expect(dialog.getByRole("alert")).toHaveText("Storage unavailable");
  await expect(confirm).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
});

test("editor shell keeps CodeMirror and dirty-buffer guard", async ({ page }) => {
  await page.goto("/README.md?edit");
  const editor = page.getByRole("textbox");
  await expect(editor).toContainText("Browser regression fixture");
  await expect(editor).toBeFocused();
  await editor.press("End");
  await editor.type("\nUpdated");
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  await page.getByRole("button", { name: "Back to files" }).click();
  await expect(page.getByRole("dialog")).toContainText("Discard unsaved changes?");
  await page.getByRole("button", { name: "Keep editing" }).click();
  await expect(editor).toContainText("Updated");
});

test("editor discard dialog owns focus and restores it", async ({ page }) => {
  await page.goto("/README.md?edit");
  const editor = page.getByRole("textbox");
  await editor.press("End");
  await editor.type("\nUpdated");

  const back = page.getByRole("button", { name: "Back to files" });
  await back.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Discard unsaved changes?" });
  await expect(dialog.getByRole("button", { name: "Discard changes" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(back).toBeFocused();
  await expect(editor).toContainText("Updated");
});

test("responsive browser has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  await expect(page.getByText("anonymous", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Account: anonymous" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New folder" })).toBeVisible();
});

test("editor controls keep the same appearance across browser engines", async ({ page }) => {
  await page.goto("/README.md?edit");
  const download = page.getByRole("link", { name: "Download" });
  const save = page.getByRole("button", { name: "Save" });
  await expect(download).toHaveCSS("color", "rgb(231, 234, 240)");
  await expect(download).toHaveCSS("text-decoration-line", "none");
  await expect(download).toHaveCSS("appearance", "none");
  await expect(save).toHaveCSS("background-color", "rgb(115, 95, 212)");
  await expect(page.getByText("anonymous", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Account: anonymous" })).toHaveCount(0);
});

test("approved visual states", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "Chromium owns deterministic baselines");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page).toHaveScreenshot("browser-desktop.png", { fullPage: true });
  await page.getByRole("button", { name: "Parallel uploads: 4" }).click();
  await expect(page).toHaveScreenshot("parallel-menu-desktop.png", { fullPage: true });
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto("/");
  await expect(page).toHaveScreenshot("browser-mobile.png", { fullPage: true });
});
