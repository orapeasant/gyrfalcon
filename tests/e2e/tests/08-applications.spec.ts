/**
 * 08-applications.spec.ts — Applications manage page: CRUD UI.
 */
import { test, expect } from "../fixtures/page";

test.describe("Applications manage page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/applications/manage");
  });

  test("shows All Applications header", async ({ dashPage: page }) => {
    await expect(page.getByText("All Applications")).toBeVisible();
  });

  test("New button is present", async ({ dashPage: page }) => {
    await expect(page.getByRole("button", { name: /New/i })).toBeVisible();
  });

  test("clicking New opens the creation form", async ({ dashPage: page }) => {
    await page.getByRole("button", { name: /New/i }).click();
    await expect(page.getByText("New Application")).toBeVisible();
    await expect(page.getByPlaceholder("my-cli-app")).toBeVisible();
  });

  test("Save is disabled when name is empty", async ({ dashPage: page }) => {
    await page.getByRole("button", { name: /New/i }).click();
    const saveBtn = page.getByRole("button", { name: "Save" });
    await expect(saveBtn).toBeDisabled();
  });

  test("Save becomes enabled after typing a name", async ({ dashPage: page }) => {
    await page.getByRole("button", { name: /New/i }).click();
    await page.getByPlaceholder("my-cli-app").fill("test-app");
    const saveBtn = page.getByRole("button", { name: "Save" });
    await expect(saveBtn).toBeEnabled();
  });

  test("Cancel closes the form panel", async ({ dashPage: page }) => {
    await page.getByRole("button", { name: /New/i }).click();
    await page.locator("button[title='Cancel']").click();
    await expect(page.getByText("New Application")).not.toBeVisible();
  });

  test("clicking enabled app opens detail page", async ({ dashPage: page }) => {
    // Only test if apps exist
    const items = page.locator("div[style*='border-bottom']").filter({ hasText: /v\d/ });
    const count = await items.count();
    if (count > 0) {
      await items.first().click();
      await expect(page.getByText("Applications")).toBeVisible();
    }
  });
});
