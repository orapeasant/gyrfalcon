/**
 * Gyrfalcon TUI — Entry point.
 * Spawns the Python gateway process and renders the Ink app.
 */
import { render } from "ink";
import React from "react";
import { App } from "./app.js";
import { GatewayClient } from "./gatewayClient.js";

async function main() {
  const client = new GatewayClient();
  await client.connect();

  const { waitUntilExit } = render(<App client={client} />);

  process.on("SIGINT", () => {
    client.disconnect();
    process.exit(0);
  });

  process.on("SIGTERM", () => {
    client.disconnect();
    process.exit(0);
  });

  await waitUntilExit();
  client.disconnect();
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
