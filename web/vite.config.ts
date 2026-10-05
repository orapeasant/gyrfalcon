import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.dirname(fileURLToPath(import.meta.url));
const requestedApp = process.env.GYRFALCON_APP_KEY;
const appKey = requestedApp && ["aie", "wf", "fnd"].includes(requestedApp)
  ? requestedApp
  : undefined;
const appRoot = appKey ? path.resolve(webRoot, "../apps", appKey, "web") : webRoot;
const webModules = path.resolve(webRoot, "node_modules");

export default defineConfig({
  root: appRoot,
  base: appKey ? `/_apps/${appKey}/` : "/",
  plugins: [react()],
  // Module sources live outside `web/`, where dependencies are installed.
  // Keep these aliases active for the compatibility build as well as the
  // app-specific builds so imports from apps/*/web resolve consistently.
  resolve: {
    alias: [
      { find: "react", replacement: path.join(webModules, "react") },
      { find: "react-dom", replacement: path.join(webModules, "react-dom") },
      { find: "react-router-dom", replacement: path.join(webModules, "react-router-dom") },
      { find: "lucide-react", replacement: path.join(webModules, "lucide-react") },
      { find: "@xyflow/react", replacement: path.join(webModules, "@xyflow/react") },
    ],
    dedupe: ["react", "react-dom"],
  },
  server: {
    proxy: {
      "/api": "http://localhost:9119",
    },
  },
  build: {
    outDir: appKey ? path.resolve(webRoot, "dist", appKey) : "dist",
    // The compatibility build writes to web/dist and must preserve the
    // independently built app bundles in web/dist/{aie,wf,fnd}. An app build
    // may clean only its own output directory.
    emptyOutDir: !!appKey,
    sourcemap: true,
  },
});
