import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TracingMethods } from "./TracingMethods";
import type { RuntimeConfig, TraceMethodInfo } from "@shared-types/runtime";

vi.mock("../../api", () => ({
  fetchTraceMethods: vi.fn(),
  fetchRuntimeConfig: vi.fn(),
}));

const APV_LIVE_FILE: TraceMethodInfo = {
  id: "apv_live_file",
  label: "Live trace (apv_trace)",
  description: "Glows nodes while calls are running.",
  warning: "Needs the apv_trace helper running inside the program you trace.",
  liveState: true,
  setupHint: '$env:APV_TRACE_LOG_PATH = "{path}"',
  settingsSchema: {
    properties: { path: { type: "string", title: "Trace log path", default: "default.jsonl" } },
  },
};

const EMPTY_CONFIG: RuntimeConfig = { methods: [], sources: [] };

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TracingMethods", () => {
  it("shows no interactive rows (still 'Loading…') while GET /runtime/config is still in flight", async () => {
    // Regression test: `fetchTraceMethods` resolving must NOT make
    // checkboxes appear before `fetchRuntimeConfig`'s result (used to
    // seed their checked state) also comes back -- a click in that gap
    // used to get silently overwritten the moment resolution finished.
    // A controllable, not-yet-resolved promise stands in for a slow
    // GET /runtime/config here.
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    let resolveConfig!: (value: RuntimeConfig) => void;
    vi.mocked(fetchRuntimeConfig).mockReturnValue(
      new Promise((resolve) => {
        resolveConfig = resolve;
      }),
    );

    render(<TracingMethods onChange={vi.fn()} />);

    // fetchTraceMethods has resolved (it was a plain mockResolvedValue),
    // but fetchRuntimeConfig hasn't -- no row should be renderable yet.
    await waitFor(() => expect(fetchTraceMethods).toHaveBeenCalled());
    expect(screen.queryByLabelText("Live trace (apv_trace)")).not.toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    resolveConfig(EMPTY_CONFIG);

    await waitFor(() => expect(screen.getByLabelText("Live trace (apv_trace)")).toBeInTheDocument());
  });

  it("renders a row per method from GET /runtime/methods, with the warning visible while unchecked", async () => {
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);

    render(<TracingMethods onChange={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("Live trace (apv_trace)")).toBeInTheDocument());
    expect(screen.getByText("Glows nodes while calls are running.")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    // Warning is visible even though the checkbox is still unchecked.
    expect(
      screen.getByText("Needs the apv_trace helper running inside the program you trace."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Trace log path")).not.toBeInTheDocument();
  });

  it("shows the generated settings fields and setup hint once the row is checked", async () => {
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);
    const user = userEvent.setup();

    render(<TracingMethods onChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Live trace (apv_trace)")).toBeInTheDocument());

    await user.click(screen.getByLabelText("Live trace (apv_trace)"));

    const pathInput = screen.getByLabelText("Trace log path") as HTMLInputElement;
    expect(pathInput.value).toBe("default.jsonl");
    // The setup hint's {path} placeholder is filled from that same value.
    expect(screen.getByText('$env:APV_TRACE_LOG_PATH = "default.jsonl"')).toBeInTheDocument();
  });

  it("fills the setup hint's {path} placeholder as the field is edited", async () => {
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);
    const user = userEvent.setup();

    render(<TracingMethods onChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Live trace (apv_trace)")).toBeInTheDocument());
    await user.click(screen.getByLabelText("Live trace (apv_trace)"));

    const pathInput = screen.getByLabelText("Trace log path");
    await user.clear(pathInput);
    await user.type(pathInput, "custom.jsonl");

    expect(screen.getByText('$env:APV_TRACE_LOG_PATH = "custom.jsonl"')).toBeInTheDocument();
  });

  it("reports checked methods via onChange, and seeds from GET /runtime/config when localStorage is empty", async () => {
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue({
      methods: [{ id: "apv_live_file", settings: { path: "env-default.jsonl" } }],
      sources: [],
    });
    const onChange = vi.fn();

    render(<TracingMethods onChange={onChange} />);

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith([{ id: "apv_live_file", settings: { path: "env-default.jsonl" } }]),
    );
    expect(await screen.findByLabelText("Trace log path")).toHaveValue("env-default.jsonl");
  });

  it("prefers a saved localStorage config over GET /runtime/config", async () => {
    localStorage.setItem(
      "apv.runtime.methods",
      JSON.stringify([{ id: "apv_live_file", settings: { path: "saved.jsonl" } }]),
    );
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);

    render(<TracingMethods onChange={vi.fn()} />);

    expect(await screen.findByLabelText("Trace log path")).toHaveValue("saved.jsonl");
    expect(fetchRuntimeConfig).not.toHaveBeenCalled();
  });

  it("shows the putErrorMessage banner when the parent passes one", async () => {
    const { fetchTraceMethods, fetchRuntimeConfig } = await import("../../api");
    vi.mocked(fetchTraceMethods).mockResolvedValue([APV_LIVE_FILE]);
    vi.mocked(fetchRuntimeConfig).mockResolvedValue(EMPTY_CONFIG);

    render(<TracingMethods onChange={vi.fn()} putErrorMessage="unknown trace method id" />);

    expect(await screen.findByText("unknown trace method id")).toBeInTheDocument();
  });
});
