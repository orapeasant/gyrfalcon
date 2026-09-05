# AI Foundry — End-to-End Tests (Playwright)

Browser-level tests for the AI Foundry dashboard using [Playwright](https://playwright.dev/).

## Prerequisites

- Node.js 18+
- The AI Foundry dashboard running (default: `http://localhost:9119`)

## Setup

```bash
cd tests/e2e
npm install
npm run install:browsers   # downloads Chromium (and Firefox if needed)
```

## Running Tests

### Against a running dashboard (default)

```bash
# Headless (CI-style)
DASHBOARD_TOKEN=<your-token> npm test

# Headed (watch the browser)
DASHBOARD_TOKEN=<your-token> npm run test:headed

# Interactive UI mode (recommended for local dev)
DASHBOARD_TOKEN=<your-token> npm run test:ui

# Debug a single test
DASHBOARD_TOKEN=<your-token> npm run test:debug -- tests/01-navigation.spec.ts
```

### Finding your DASHBOARD_TOKEN

The token is printed by the dashboard on startup, or stored at:

```
~/.gyrfalcon/.dashboard_token
```

If no token is provided, the fixture tries to read it from that file automatically.

### Custom base URL

```bash
BASE_URL=http://myserver:9119 DASHBOARD_TOKEN=xxx npm test
```

## Test Structure

```
tests/e2e/
├── playwright.config.ts       # Playwright configuration
├── package.json
├── fixtures/
│   └── page.ts                # Shared dashPage fixture (token injection)
└── tests/
    ├── 01-navigation.spec.ts  # Sidebar nav, routing, collapse
    ├── 02-theme.spec.ts       # Light/dark theme toggle + persistence
    ├── 03-sessions.spec.ts    # Sessions pagination, search, delete
    ├── 04-analytics.spec.ts   # Analytics table, date ordering
    ├── 05-mcp.spec.ts         # MCP Servers list, form, auth types
    ├── 06-skills.spec.ts      # Skills list, create, editor
    ├── 07-cron.spec.ts        # Cron jobs, Run button, navigation
    └── 08-applications.spec.ts # Applications CRUD UI
```

## Viewing Reports

After a test run:

```bash
npm run test:report
```

Opens the HTML report with screenshots and traces for failed tests.

## CI Integration

Set these environment variables in your CI pipeline:

| Variable | Description |
|---|---|
| `DASHBOARD_TOKEN` | Session token for auth |
| `BASE_URL` | Dashboard URL (default: `http://localhost:9119`) |
| `CI` | Set to `true` to enable retries and stricter behaviour |

Example GitHub Actions step:

```yaml
- name: Run E2E tests
  working-directory: tests/e2e
  run: |
    npm ci
    npm run install:browsers
    npm test
  env:
    BASE_URL: http://localhost:9119
    DASHBOARD_TOKEN: ${{ secrets.DASHBOARD_TOKEN }}
    CI: true
```
