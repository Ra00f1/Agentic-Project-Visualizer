import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { E2E_TRACE_LOG_PATH } from "./tests/e2e/trace-log-path";

const __dirname = dirname(fileURLToPath(import.meta.url));

// Backend + frontend both need to be up for the runtime-overlay e2e spec.
// Playwright starts and tears down both; the backend is pointed at a
// scratch trace-log path (see trace-log-path.ts) so the spec can feed it a
// scripted event stream without touching a real target project.
export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    // Must be "localhost", not "127.0.0.1" -- the backend's CORS allow-list
    // (Settings.cors_origins, config.py) only permits "http://localhost:1420"
    // (the Tauri dev-server origin). A same-machine-but-different-hostname
    // origin is a different CORS origin as far as the browser is concerned,
    // so every fetchSchema/fetchGraph call from the page would otherwise be
    // silently blocked and the Setup screen would never leave "loading".
    baseURL: "http://localhost:1420",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "uv run uvicorn agentic_visualizer.main:app --host 127.0.0.1 --port 8765",
      cwd: resolve(__dirname, "../backend"),
      url: "http://127.0.0.1:8765/health",
      reuseExistingServer: false,
      env: { AGENTIC_DEFAULT_TRACE_LOG_PATH: E2E_TRACE_LOG_PATH },
      timeout: 30_000,
    },
    {
      command: "npm run dev",
      cwd: __dirname,
      url: "http://localhost:1420",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
