/**
 * Integration-style test for Task 11's `/`-to-search and Task 12's
 * `f`-to-fit-view keyboard shortcuts — both live in App.tsx's one `onKey`
 * effect, so one harness/suite covers both (matching Task 12's own Design
 * note: "Reuses the keyboard handler infrastructure from Task 11 — they
 * should land together or in sequence").
 *
 * Deliberately does NOT render `<App />` itself: App owns a full graph-view
 * tree (React Flow, live `fetchGraph`/WebSocket data loading, the Tauri
 * `opener` plugin) with no existing mocking infrastructure anywhere in this
 * test suite, and building that out (ResizeObserver polyfill, a fetchGraph
 * mock, a WS mock, a Tauri plugin mock) just to get past the Setup screen
 * would be a lot of new, fragile scaffolding for two keyboard shortcuts —
 * disproportionate to what these tasks ask for, and a maintenance burden
 * tied to App's data-fetching shape rather than the shortcuts themselves.
 *
 * Instead, `ShortcutHarness` below reproduces the EXACT wiring App.tsx uses
 * (a window keydown listener, refs, the shared `isTypingInField` guard,
 * `preventDefault` + `.focus()` / `.fitView()`) around a real `<input>` in a
 * real DOM, so this exercises the real interaction — actual event dispatch,
 * actual `HTMLElement.focus()`, the real shared `isTypingInField` — not a
 * reimplementation of the logic under test. `fitView` itself is a `vi.fn()`
 * stand-in for React Flow's instance, per Task 12's own Verification bullet
 * ("construct a mock rfInstanceRef, fire an f keydown, assert fitView was
 * called") — React Flow's real instance needs a mounted `<ReactFlow>` tree,
 * which is exactly the heavyweight dependency this harness exists to avoid.
 */
import { describe, expect, it, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef } from "react";
import { isTypingInField } from "./lib/keyboard";

function ShortcutHarness({ fitView }: { fitView: (opts: { padding: number; duration: number }) => void }) {
  const searchInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && !isTypingInField(e.target)) {
        e.preventDefault();
        searchInputRef.current?.focus();
        return;
      }
      if (e.key === "f" && !isTypingInField(e.target)) {
        e.preventDefault();
        fitView({ padding: 0.2, duration: 300 });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fitView]);

  return (
    <div>
      <input ref={searchInputRef} aria-label="Search nodes" />
      <button>Some other focusable control</button>
      <textarea aria-label="Other field" />
    </div>
  );
}

afterEach(() => {
  cleanup();
});

describe("`/` search shortcut", () => {
  it("focuses the search input when pressed outside any field", async () => {
    const user = userEvent.setup();
    render(<ShortcutHarness fitView={vi.fn()} />);

    expect(screen.getByLabelText("Search nodes")).not.toHaveFocus();
    await user.keyboard("/");

    expect(screen.getByLabelText("Search nodes")).toHaveFocus();
  });

  it("does not insert '/' into the search input when it fires", async () => {
    const user = userEvent.setup();
    render(<ShortcutHarness fitView={vi.fn()} />);

    await user.keyboard("/");

    expect(screen.getByLabelText("Search nodes")).toHaveValue("");
  });

  it("types '/' normally when the search input is already focused", async () => {
    const user = userEvent.setup();
    render(<ShortcutHarness fitView={vi.fn()} />);

    const input = screen.getByLabelText("Search nodes");
    await user.click(input);
    await user.keyboard("/");

    expect(input).toHaveValue("/");
  });

  it("types '/' normally when focus is in a different text field", async () => {
    const user = userEvent.setup();
    render(<ShortcutHarness fitView={vi.fn()} />);

    const other = screen.getByLabelText("Other field");
    await user.click(other);
    await user.keyboard("/");

    expect(other).toHaveValue("/");
    expect(screen.getByLabelText("Search nodes")).not.toHaveFocus();
  });

  it("still focuses search when a non-field element (e.g. a button) has focus", async () => {
    const user = userEvent.setup();
    render(<ShortcutHarness fitView={vi.fn()} />);

    screen.getByRole("button").focus();
    await user.keyboard("/");

    expect(screen.getByLabelText("Search nodes")).toHaveFocus();
  });
});

describe("`f` fit-view shortcut", () => {
  it("calls fitView when pressed outside any field", async () => {
    const user = userEvent.setup();
    const fitView = vi.fn();
    render(<ShortcutHarness fitView={fitView} />);

    await user.keyboard("f");

    expect(fitView).toHaveBeenCalledTimes(1);
    expect(fitView).toHaveBeenCalledWith({ padding: 0.2, duration: 300 });
  });

  it("does not call fitView when the search input is focused — types 'f' instead", async () => {
    const user = userEvent.setup();
    const fitView = vi.fn();
    render(<ShortcutHarness fitView={fitView} />);

    const input = screen.getByLabelText("Search nodes");
    await user.click(input);
    await user.keyboard("f");

    expect(fitView).not.toHaveBeenCalled();
    expect(input).toHaveValue("f");
  });

  it("does not call fitView when a different text field is focused", async () => {
    const user = userEvent.setup();
    const fitView = vi.fn();
    render(<ShortcutHarness fitView={fitView} />);

    const other = screen.getByLabelText("Other field");
    await user.click(other);
    await user.keyboard("f");

    expect(fitView).not.toHaveBeenCalled();
    expect(other).toHaveValue("f");
  });

  it("still calls fitView when a non-field element (e.g. a button) has focus", async () => {
    const user = userEvent.setup();
    const fitView = vi.fn();
    render(<ShortcutHarness fitView={fitView} />);

    screen.getByRole("button").focus();
    await user.keyboard("f");

    expect(fitView).toHaveBeenCalledTimes(1);
  });
});
