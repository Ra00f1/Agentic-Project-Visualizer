import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Separate from vite.config.ts on purpose: that file is an async factory
// with Tauri-dev-server-specific settings (fixed port, HMR over a
// Tauri-injected host) that have no meaning for a test run and aren't
// worth threading test-only concerns through. Vitest reads this file
// directly (`vitest` picks up `vitest.config.ts` over `vite.config.ts`
// when both exist), so the two stay independent without duplicating the
// `@shared-types` alias story badly -- it's just one line here too.
const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@shared-types": resolve(__dirname, "../../packages/shared-types"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    globals: false,
    // tests/e2e/**/*.spec.ts are Playwright specs (run via `npx playwright
    // test`, own config in playwright.config.ts) -- Vitest's default
    // include glob otherwise picks them up too and fails immediately since
    // `test.beforeAll` there is Playwright's, not Vitest's.
    exclude: ["**/node_modules/**", "tests/e2e/**"],
  },
});
