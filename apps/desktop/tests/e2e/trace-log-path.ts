import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Shared by `playwright.config.ts` (which points the backend's
 * `AGENTIC_DEFAULT_TRACE_LOG_PATH` at this file) and the spec itself (which
 * appends scripted `TraceEvent` JSONL lines to it). Lives outside the repo
 * tree, in the OS temp dir, so a leftover file from an interrupted run never
 * shows up as an untracked file in `git status`.
 *
 * Unique per `npx playwright test` invocation, not a fixed name: Playwright's
 * `webServer` entries start and become healthy BEFORE `globalSetup` runs --
 * the reverse of what an earlier version of this file assumed. With a fixed
 * path, the backend's `FileTailTraceSource` could already be open, seeked to
 * the end of a PREVIOUS run's leftover content, by the time `globalSetup`
 * (in `global-setup.ts`) truncated that same path out from under the live
 * handle -- a real race, not a hypothetical one; it's what motivated the
 * truncation-recovery fix in the backend's `file_tail.py`. A unique path per
 * run means there is never leftover content to race against in the first
 * place. The backend fix stays (it's a real capability for an actual target
 * project's `copytruncate`-style log rotator, independent of this test
 * harness), but this removes the reason THIS suite depended on it firing.
 *
 * Computed once and cached in `process.env`, not recomputed on every
 * import: `playwright.config.ts` and each test file run in SEPARATE
 * processes (the CLI's main process builds the config; tests run in worker
 * processes it spawns). A naive `${process.pid}-${Date.now()}` computed
 * independently in each would produce two DIFFERENT paths -- the backend
 * would end up watching one file while the spec appends to another, and
 * every assertion would time out. Setting `process.env` here in the config
 * process, before any worker is spawned, relies on Node's default behavior
 * of inheriting the parent's environment into child processes, so every
 * worker's own import of this module reads the SAME value the config
 * process already committed to.
 *
 * Forward slashes even on Windows: `path.join` would give back a
 * backslash-separated path, which is fine for Node's own fs calls but goes
 * through an extra hop here -- it ends up in an env var read by
 * `pathlib.Path` on the Python side. Forward slashes work there natively,
 * and avoiding backslashes sidesteps ever having to reason about whether
 * something in that chain (shell quoting, env-var encoding) treats `\`
 * specially.
 */
const ENV_KEY = "APV_E2E_TRACE_LOG_PATH";

function resolveTraceLogPath(): string {
  const existing = process.env[ENV_KEY];
  if (existing) return existing;
  const fresh = join(tmpdir(), `apv-e2e-runtime-trace-${process.pid}-${Date.now()}.jsonl`).replaceAll(
    "\\",
    "/",
  );
  process.env[ENV_KEY] = fresh;
  return fresh;
}

export const E2E_TRACE_LOG_PATH = resolveTraceLogPath();
