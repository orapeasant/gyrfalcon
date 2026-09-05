/**
 * 07-scheduler.spec.ts — Scheduler page: job list, run button, create form.
 */
import { test, expect } from "../fixtures/page";

test.describe("Scheduler page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/scheduler");
  });

  test("shows Scheduled Jobs header", async ({ dashPage: page }) => {
    await expect(page.getByText("Scheduled Jobs")).toBeVisible();
  });

  test("New button is present", async ({ dashPage: page }) => {
    const newBtn = page.getByRole("button", { name: /New/i });
    await expect(newBtn).toBeVisible();
  });

  test("clicking New opens the editor", async ({ dashPage: page }) => {
    await page.getByRole("button", { name: /New/i }).click();
    await expect(page.getByPlaceholder(/Job name/i)).toBeVisible({ timeout: 3000 });
  });

  test("each job row shows a Run (Zap) button", async ({ dashPage: page }) => {
    const rows = page.locator("button[title='Run now — opens as chat session']");
    const count = await rows.count();
    // Only check layout — not whether jobs exist
    if (count > 0) {
      await expect(rows.first()).toBeVisible();
    }
  });

  test("clicking Run button shows toast and navigates to chat", async ({ dashPage: page }) => {
    const runBtns = page.locator("button[title='Run now — opens as chat session']");
    const count = await runBtns.count();
    if (count === 0) {
      test.skip(true, "No scheduled jobs configured — skipping run test");
      return;
    }
    await runBtns.first().click();
    // Toast should appear
    const toast = page.locator("text=started — opening session");
    await expect(toast).toBeVisible({ timeout: 5000 });
    // Should eventually navigate to /chat
    await page.waitForURL(/\/chat/, { timeout: 10000 });
  });
});
