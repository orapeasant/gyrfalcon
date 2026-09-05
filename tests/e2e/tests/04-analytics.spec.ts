/**
 * 04-analytics.spec.ts — Analytics page: table, sorting, date order.
 */
import { test, expect } from "../fixtures/page";

test.describe("Analytics page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/analytics");
  });

  test("shows Analytics heading", async ({ dashPage: page }) => {
    await expect(page.getByText("Analytics").first()).toBeVisible();
  });

  test("shows column headers when data exists", async ({ dashPage: page }) => {
    const table = page.locator("table");
    const hasTable = await table.isVisible().catch(() => false);
    if (hasTable) {
      await expect(page.getByText("Day")).toBeVisible();
      await expect(page.getByText("Sessions")).toBeVisible();
      await expect(page.getByText("Cost")).toBeVisible();
    } else {
      await expect(page.getByText("No usage data yet")).toBeVisible();
    }
  });

  test("dates are in descending order", async ({ dashPage: page }) => {
    const table = page.locator("table");
    const hasTable = await table.isVisible().catch(() => false);
    if (!hasTable) return; // no data — skip

    const cells = page.locator("tbody tr td:first-child");
    const texts = await cells.allTextContents();
    const dates = texts.filter(t => /\d{4}-\d{2}-\d{2}/.test(t));
    if (dates.length < 2) return;

    for (let i = 0; i < dates.length - 1; i++) {
      expect(new Date(dates[i]).getTime()).toBeGreaterThanOrEqual(
        new Date(dates[i + 1]).getTime()
      );
    }
  });
});
