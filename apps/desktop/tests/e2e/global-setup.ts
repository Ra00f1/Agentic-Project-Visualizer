import { writeFileSync } from "node:fs";
import { E2E_TRACE_LOG_PATH } from "./trace-log-path";

/**
 * Runs once per `npx playwright test` invocation. Playwright starts the
 * `webServer` entries (and waits for each to report healthy) BEFORE running
 * `globalSetup` — the reverse of what an earlier version of this comment
 * claimed. That ordering is exactly why `trace-log-path.ts` makes
 * `E2E_TRACE_LOG_PATH` unique per run rather than a fixed name: with a fixed
 * path, the backend's `FileTailTraceSource` could already be open and
 * seeked to the end of a PREVIOUS run's content by the time this function's
 * truncate landed on it. A unique path removes that race at its source —
 * this file simply doesn't exist anywhere until this run creates it.
 *
 * Pre-creating the (empty) file here, rather than letting the spec's own
 * first `appendFileSync` call create it on demand, still earns its keep:
 * it gives the backend's reader a real, multi-second head start (through
 * both webServer readiness checks and the first test's page load) to reach
 * its steady "opened, seeked to end-of-empty-file, polling" state BEFORE
 * anything is ever written to the path. Skipping this and letting the first
 * `appendFileSync` both create the file AND write to it in the same call
 * would reintroduce the same class of race this whole file exists to avoid
 * — the reader could observe the file as already non-empty the moment it
 * first sees it exist, and (by design, `from_beginning` defaults to false)
 * skip straight past that first write.
 */
export default function globalSetup(): void {
  writeFileSync(E2E_TRACE_LOG_PATH, "");
}
