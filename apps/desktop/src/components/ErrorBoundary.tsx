/**
 * ErrorBoundary — catches render errors in the graph subtree.
 *
 * React only catches errors thrown during rendering through class-component
 * error boundaries; hooks can't do this. So this is a small class component
 * that:
 *   * catches errors via `getDerivedStateFromError` (updates local state so
 *     the fallback UI mounts on the next render),
 *   * logs them via `componentDidCatch` (which fires with a React info
 *     object — includes the component stack),
 *   * renders a small in-place fallback with the message and a Reset
 *     button, so a bug in EntityNode (say a missing NODE_STYLE entry for
 *     a novel node type) doesn't white-screen the whole app.
 *
 * The fallback is intentionally small — it fits inside the graph pane
 * where the crash happened, keeps the header and side panel intact, and
 * doesn't try to recover automatically (which would loop if the underlying
 * data is what's bad).
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("Graph render error caught by boundary:", error, info.componentStack);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        role="alert"
        className="flex h-full w-full flex-col items-center justify-center gap-3 p-8 text-center text-neutral-800 dark:text-neutral-200"
      >
        <div className="rounded-full bg-red-100 p-3 text-red-700 dark:bg-red-950 dark:text-red-300">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 9v4M12 17h.01M4.93 19h14.14a2 2 0 0 0 1.72-3L13.72 4.87a2 2 0 0 0-3.44 0L3.21 16a2 2 0 0 0 1.72 3z" />
          </svg>
        </div>
        <h2 className="text-lg font-semibold">The graph couldn't render.</h2>
        <p className="max-w-md text-sm text-neutral-600 dark:text-neutral-400">
          Something went wrong drawing the graph. Try reloading — if this keeps
          happening, hit Refresh in the header to fetch a fresh scan.
        </p>
        <pre className="max-w-lg overflow-auto rounded border border-neutral-200 bg-neutral-50 p-3 text-left font-mono text-[11px] text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
          {error.message}
        </pre>
        <button
          onClick={this.handleReset}
          className="rounded bg-neutral-900 px-4 py-1.5 text-sm text-white dark:bg-neutral-100 dark:text-neutral-900"
        >
          Try again
        </button>
      </div>
    );
  }
}
