/**
 * 06-skills.spec.ts — Skills page: list panel, create, editor.
 */
import { test, expect } from "../fixtures/page";

test.describe("Skills page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/skills");
  });

  test("shows Skills panel", async ({ dashPage: page }) => {
    await expect(page.getByText("Skills").first()).toBeVisible();
  });

  test("shows + button to create new skill", async ({ dashPage: page }) => {
    const plusBtn = page.locator("button[title='New skill']");
    await expect(plusBtn).toBeVisible();
  });

  test("clicking + opens new skill form", async ({ dashPage: page }) => {
    await page.locator("button[title='New skill']").click();
    await expect(page.getByText("New Skill")).toBeVisible();
    await expect(page.getByPlaceholder(/skill-name/i)).toBeVisible();
  });

  test("cancel closes the new skill form", async ({ dashPage: page }) => {
    await page.locator("button[title='New skill']").click();
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByText("New Skill")).not.toBeVisible();
  });

  test("skill list items are clickable", async ({ dashPage: page }) => {
    const items = page.locator("div[style*='border-left']").filter({ hasText: /📄/ });
    const count = await items.count();
    if (count > 0) {
      await items.first().click();
      // Editor area should appear
      await expect(page.locator("textarea")).toBeVisible({ timeout: 3000 });
    }
  });
});
