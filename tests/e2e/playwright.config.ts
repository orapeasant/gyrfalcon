import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for AI Foundry dashboard e2e tests.
 *
 * Targets the running dashboard server (default: http://localhost:9119).
 * Override with BASE_URL env var:
 *   BASE_URL=http://localhost:9119 npm test
 *
 * The dashboard requires a session token injected into window.__GYRFALCON_SESSION_TOKEN__.
 * Tests use the storageState fixture (fixtures/auth.ts) to inject it from
 * the DASHBOARD_TOKEN env var or fall back to a dev-mode token.
 */
export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  fullyParallel: false,       // dashboard is stateful — run serially by default
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],

  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:9119",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",

    // Inject session token into every page via init script
    // Token is read from DASHBOARD_TOKEN env var (set by start-dashboard script)
    // or from the __token.txt file written by the dashboard on startup.
    ...( process.env.DASHBOARD_TOKEN
      ? {
          extraHTTPHeaders: {
            "X-Gyrfalcon-Session-Token": process.env.DASHBOARD_TOKEN,
          },
        }
      : {} ),
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "firefox",
      use: { ...devices["Desktop Firefox"] },
    },
  ],
});
