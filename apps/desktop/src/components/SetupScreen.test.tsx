/**
 * Task 22, Verification 11: "Continue saves to localStorage and calls
 * PUT. A 422 shows the field error."
 *
 * Scoped to `handleContinue`'s own wiring -- the Mongo-collections side
 * of Setup (role/field-mapping UI) is already covered by this repo's
 * earlier tasks and isn't re-tested here. `fetchSchema` is mocked to
 * resolve a single collection just to make the Continue button enabled
 * (`canContinue` requires `status === "ready" && totalSelected > 0`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SetupScreen } from "./SetupScreen";
import type { CollectionSchema } from "@shared-types/graph";
import type { RuntimeConfig, TraceMethodInfo } from "@shared-types/runtime";

// TracingMethods.tsx imports this same module as `"../../api"` (it lives
// one directory deeper, at src/components/setup/) -- both specifiers
// resolve to the identical src/api.ts file, so mocking it once here, by
// THIS test file's own relative path to it, covers both SetupScreen's
// and TracingMethods' calls.
vi.mock("../api", () => ({
  fetchSchema: vi.fn(),
  putRuntimeConfig: vi.fn(),
  fetchTraceMethods: vi.fn(),
  fetchRuntimeConfig: vi.fn(),
}));

const ONE_COLLECTION: CollectionSchema[] = [
  { name: "workflows", documentCount: 3, fieldNames: ["_id", "name"] },
];

const EMPTY_METHODS: TraceMethodInfo[] = [];
const EMPTY_CONFIG: RuntimeConfig = { methods: [], sources: [] };

async function renderReady() {
  const { fetchSchema, fetchTraceMethods, fetchRuntimeConfig } = await import("../api");
  vi.mocked(fetchSchema).mockResolvedValue(ONE_COLLECTION);
  vi.mocked(fetchTraceMethods).mockResolvedValue(EMPTY_METHODS);
  vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);

  const onSubmit = vi.fn();
  render(
    <SetupScreen initialUri="mongodb://localhost:27017" initialDb="mock_agent" initialCodebaseRoot="" onSubmit={onSubmit} />,
  );
  const button = await screen.findByRole("button", { name: /Continue/ });
  await waitFor(() => expect(button).toBeEnabled());
  return { onSubmit, button };
}

beforeEach(() => {
  localStorage.clear();
  vi.useRealTimers();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SetupScreen Continue", () => {
  it("saves the tracing config to localStorage, PUTs it, then calls onSubmit on success", async () => {
    const { putRuntimeConfig } = await import("../api");
    vi.mocked(putRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);
    const user = userEvent.setup();
    const { onSubmit, button } = await renderReady();

    await user.click(button);

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(putRuntimeConfig).toHaveBeenCalledWith({ methods: [] });
    expect(localStorage.getItem("apv.runtime.methods")).toBe("[]");
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ uri: "mongodb://localhost:27017", db: "mock_agent" }),
    );
  });

  it("keeps Setup open and shows the error instead of calling onSubmit when PUT fails (422)", async () => {
    const { putRuntimeConfig } = await import("../api");
    vi.mocked(putRuntimeConfig).mockRejectedValue(
      new Error('PUT /runtime/config failed (422): {"id":"apv_live_file","errors":[...]}'),
    );
    const user = userEvent.setup();
    const { onSubmit, button } = await renderReady();

    await user.click(button);

    await waitFor(() =>
      expect(screen.getByText(/PUT \/runtime\/config failed \(422\)/)).toBeInTheDocument(),
    );
    expect(onSubmit).not.toHaveBeenCalled();
    // The button is interactive again -- the user can fix the settings and retry.
    expect(screen.getByRole("button", { name: /Continue/ })).toBeEnabled();
  });
});
