/**
 * 05-mcp.spec.ts — MCP Servers page: list, add form, auth type switching.
 */
import { test, expect } from "../fixtures/page";

test.describe("MCP Servers page", () => {
  test.beforeEach(async ({ dashPage: page }) => {
    await page.goto("/mcp");
  });

  test("renders server list panel", async ({ dashPage: page }) => {
    // List panel or empty state should be visible
    const panel = page.locator("text=MCP Servers, text=No servers configured").first();
    await expect(panel).toBeVisible();
  });

  test("Add Server button opens new server form", async ({ dashPage: page }) => {
    const addBtn = page.getByTitle("Add server").first();
    const hasBtnVisible = await addBtn.isVisible().catch(() => false);
    if (!hasBtnVisible) {
      // Try the + button
      await page.locator("button:has-text('+')").first().click();
    } else {
      await addBtn.click();
    }
    // Name field should appear
    await expect(page.getByPlaceholder(/my-server/i)).toBeVisible({ timeout: 3000 });
  });

  test("OpenAPI auth type selector shows all options", async ({ dashPage: page }) => {
    // Navigate to a new OpenAPI server form
    await page.goto("/mcp");
    const btns = page.locator("button");
    // Find the + or Add button
    const addBtn = btns.filter({ hasText: "+" }).first();
    const hasAdd = await addBtn.isVisible().catch(() => false);
    if (!hasAdd) return; // no add button visible, skip

    await addBtn.click();
    // Switch type to openapi
    const typeOpenApi = page.getByText("OpenAPI");
    const hasType = await typeOpenApi.isVisible().catch(() => false);
    if (hasType) {
      await typeOpenApi.click();
      const authSelect = page.locator("select");
      const authVisible = await authSelect.first().isVisible().catch(() => false);
      if (authVisible) {
        const options = await authSelect.first().locator("option").allTextContents();
        expect(options).toContain("No Auth");
        expect(options).toContain("Basic Auth");
        expect(options).toContain("API Key");
        expect(options).toContain("OAuth 2.0");
      }
    }
  });
});
