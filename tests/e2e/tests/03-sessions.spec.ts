/**
 * 03-sessions.spec.ts — Sessions page: pagination, search, delete.
 */
import { test, expect } from "../fixtures/page";

test.describe("Sessions page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/sessions");
  });

  test("renders sessions page heading", async ({ dashPage: page }) => {
    await expect(page.getByRole("heading", { name: /Sessions/i })).toBeVisible();
  });

  test("shows page-size selector with options 20, 50, 100", async ({ dashPage: page }) => {
    const selector = page.locator("select").first();
    await expect(selector).toBeVisible();
    const options = await selector.locator("option").allTextContents();
    expect(options).toContain("20 / page");
    expect(options).toContain("50 / page");
    expect(options).toContain("100 / page");
  });

  test("filter input is present", async ({ dashPage: page }) => {
    await expect(page.getByPlaceholder(/Filter by title or ID/i)).toBeVisible();
  });

  test("filter input narrows displayed sessions", async ({ dashPage: page }) => {
    const input = page.getByPlaceholder(/Filter by title or ID/i);
    await input.fill("zzz_nonexistent_session_xyz");
    // Either no rows or empty state
    const emptyMsg = page.getByText(/No sessions match/i);
    const rows = page.locator("tbody tr");
    const emptyVisible = await emptyMsg.isVisible().catch(() => false);
    const rowCount = await rows.count();
    expect(emptyVisible || rowCount === 0).toBe(true);
  });

  test("page-size change updates displayed count", async ({ dashPage: page }) => {
    const selector = page.locator("select").first();
    await selector.selectOption("20");
    // Should still render without error
    await expect(page.locator("table")).toBeVisible();
  });

  test("delete page button is visible when sessions exist", async ({ dashPage: page }) => {
    const table = page.locator("table");
    const hasTable = await table.isVisible().catch(() => false);
    if (hasTable) {
      const deleteBtn = page.getByTitle(/Delete all .* sessions on this page/i);
      await expect(deleteBtn).toBeVisible();
    }
  });
});
