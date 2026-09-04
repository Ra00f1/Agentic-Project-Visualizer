import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Node ESM equivalent of __dirname — needed because this config is a module.
const __dirname = dirname(fileURLToPath(import.meta.url));

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react(), tailwindcss()],

  // Mirror the tsconfig `paths` alias so Vite can actually resolve
  // `@shared-types/*` imports at bundle time. If these two ever drift
  // apart, tsc will happily typecheck and Vite will fail at build with
  // "module not found" — keep them in sync.
  resolve: {
    alias: {
      "@shared-types": resolve(__dirname, "../../packages/shared-types"),
    },
  },

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
    // Serving files from the sibling `packages/` folder requires an explicit
    // allow-list — Vite defaults to only the project root for security.
    fs: {
      allow: [resolve(__dirname, "../.."), __dirname],
    },
  },
}));
