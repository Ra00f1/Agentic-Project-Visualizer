/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend base URL outside Tauri (plain browser / vitest). See
   *  `src/lib/backend.ts`. Falls back to the backend's own default. */
  readonly VITE_APV_BACKEND_URL?: string;
  /** Session token to pair with VITE_APV_BACKEND_URL outside Tauri. */
  readonly VITE_APV_SESSION_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
