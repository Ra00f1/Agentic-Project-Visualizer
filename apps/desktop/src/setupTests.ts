import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// React Testing Library's auto-cleanup relies on detecting a global
// `afterEach` — since vitest.config.ts sets `test.globals: false`
// (deliberately, so test files import `describe`/`it`/etc. explicitly
// rather than relying on ambient globals), that detection doesn't fire.
// Without this, each test's rendered DOM stays mounted into the next
// test in the same file, causing duplicate-element query failures.
afterEach(() => {
  cleanup();
});
