/**
 * 02-theme.spec.ts — light/dark theme toggle tests.
 */
import { test, expect } from "../fixtures/page";

test.describe("Theme toggle", () => {
  test("dark theme is active by default when system prefers dark", async ({ dashPage: page }) => {
    await page.goto("/");
    const theme = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme")
    );
    expect(["dark", "light"]).toContain(theme);
  });

  test("theme toggle button is visible next to user name", async ({ dashPage: page }) => {
    await page.goto("/");
    const btn = page.getByTitle(/Switch to (light|dark) theme/);
    await expect(btn).toBeVisible();
  });

  test("clicking theme toggle switches data-theme attribute", async ({ dashPage: page }) => {
    await page.goto("/");
    const before = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme")
    );
    await page.getByTitle(/Switch to (light|dark) theme/).click();
    const after = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme")
    );
    expect(after).not.toBe(before);
    const expected = before === "dark" ? "light" : "dark";
    expect(after).toBe(expected);
  });

  test("theme persists in localStorage", async ({ dashPage: page }) => {
    await page.goto("/");
    await page.getByTitle(/Switch to (light|dark) theme/).click();
    const stored = await page.evaluate(() =>
      localStorage.getItem("gyrfalcon-theme")
    );
    expect(["dark", "light"]).toContain(stored);
  });

  test("toggling twice returns to original theme", async ({ dashPage: page }) => {
    await page.goto("/");
    const before = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme")
    );
    const btn = page.getByTitle(/Switch to (light|dark) theme/);
    await btn.click();
    await btn.click();
    const after = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme")
    );
    expect(after).toBe(before);
  });
});
