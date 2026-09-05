/**
 * Shared Playwright fixtures for AI Foundry dashboard tests.
 *
 * Usage:
 *   import { test, expect } from "../fixtures/page";
 *
 * The `dashPage` fixture opens a new page and injects the session token
 * into window.__GYRFALCON_SESSION_TOKEN__ before any navigation,
 * exactly as the dashboard launcher does for the real browser.
 */
import { test as base, expect, Page } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

export { expect };

/** Resolve the session token from env or ~/.gyrfalcon/.dashboard_token */
function resolveToken(): string {
  if (process.env.DASHBOARD_TOKEN) return process.env.DASHBOARD_TOKEN;

  // Try the token file written by the dashboard on startup
  const candidates = [
    path.join(process.env.USERPROFILE || process.env.HOME || "", ".gyrfalcon", ".dashboard_token"),
    path.join(process.env.USERPROFILE || process.env.HOME || "", ".gyrfalcon", "dashboard_token.txt"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      const tok = fs.readFileSync(p, "utf-8").trim();
      if (tok) return tok;
    }
  }
  console.warn("[fixture] No DASHBOARD_TOKEN found — token injection skipped. Some tests may fail auth.");
  return "";
}

const SESSION_TOKEN = resolveToken();
const BASE_URL = process.env.BASE_URL ?? "http://localhost:9119";

type DashFixtures = {
  /** A Page with the session token already injected. */
  dashPage: Page;
};

export const test = base.extend<DashFixtures>({
  dashPage: async ({ browser }, use) => {
    const ctx = await browser.newContext({ baseURL: BASE_URL });

    // Inject token into every page opened by this context
    if (SESSION_TOKEN) {
      await ctx.addInitScript((token: string) => {
        (window as any).__GYRFALCON_SESSION_TOKEN__ = token;
      }, SESSION_TOKEN);
    }

    const page = await ctx.newPage();
    await use(page);
    await ctx.close();
  },
});
