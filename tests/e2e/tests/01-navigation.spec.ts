/**
 * 01-navigation.spec.ts — sidebar navigation and routing smoke tests.
 */
import { test, expect } from "../fixtures/page";

test.describe("Sidebar navigation", () => {
  test("loads dashboard and shows app name", async ({ dashPage: page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/AI Foundry/i);
    await expect(page.locator("text=AI Foundry").first()).toBeVisible();
  });

  test("redirects / to /chat", async ({ dashPage: page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/chat/);
  });

  test("navigates to Sessions", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("Sessions").click();
    await expect(page).toHaveURL(/\/sessions/);
    await expect(page.getByRole("heading", { name: /Sessions/i })).toBeVisible();
  });

  test("navigates to Analytics", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("Analytics").click();
    await expect(page).toHaveURL(/\/analytics/);
    await expect(page.getByText("Analytics").first()).toBeVisible();
  });

  test("navigates to Models", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("Models").click();
    await expect(page).toHaveURL(/\/models/);
  });

  test("navigates to MCP Servers", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("MCP Servers").click();
    await expect(page).toHaveURL(/\/mcp/);
  });

  test("navigates to Skills", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("Skills").click();
    await expect(page).toHaveURL(/\/skills/);
  });

  test("navigates to Scheduler", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByText("Scheduler").click();
    await expect(page).toHaveURL(/\/scheduler/);
  });

  test("sidebar collapses and expands", async ({ dashPage: page }) => {
    await page.goto("/");
    // Find collapse button
    const collapseBtn = page.getByTitle("Collapse sidebar");
    await collapseBtn.click();
    // App name text should be hidden
    await expect(page.getByText("AI Dashboard")).not.toBeVisible();
    // Expand again
    const expandBtn = page.getByTitle("Expand sidebar");
    await expandBtn.click();
    await expect(page.getByText("AI Dashboard")).toBeVisible();
  });

  test("Applications gear icon navigates to manage page", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByTitle("Manage Applications").click();
    await expect(page).toHaveURL(/\/applications\/manage/);
  });
});
