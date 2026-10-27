/**
 * Toolbar chip showing the runtime overlay's actual state.
 *
 * Exists because, before Task 21, "nothing's happening" looked identical
 * for four different reasons: the WebSocket not being connected, the
 * overlay never having been turned on, the backend tailing a file the
 * target hasn't written yet, and the overlay genuinely tailing with
 * nothing to show. This chip tells those apart.
 *
 * Task 22: the backend can run SEVERAL trace sources at once (the Setup
 * screen's "Runtime tracing" section), so this now summarizes a list
 * instead of one path/state pair -- see `chipDisplay`'s table below.
 * Telling a "live" (glows while running) source apart from an
 * after-the-fact one (Task 24) needs each source's method's `liveState`,
 * which only `GET /runtime/methods` knows (`SourceStatus` itself doesn't
 * carry it) -- fetched once here and cached for the component's lifetime,
 * since the registry is fixed for the whole backend process.
 *
 * Hand-rolled Tailwind, same convention as `DeadLetterDrawer.tsx` (shadcn
 * isn't set up in this repo).
 */

import { useEffect, useState } from "react";
import { fetchTraceMethods } from "../api";
import { useRuntimeStore, type DeadLetterStats, type RuntimeConnectionState } from "../state/runtime";
import type { SourceStatus } from "@shared-types/runtime";

interface ChipDisplay {
  dotClass: string;
  label: string;
  title?: string;
}

function sourceTooltipLine(source: SourceStatus): string {
  const parts = [source.label, source.state];
  if (source.detail) parts.push(source.detail);
  parts.push(`received ${source.eventsReceived}`, `ignored ${source.eventsIgnored}`);
  return parts.join(" · ");
}

function chipDisplay(
  connection: RuntimeConnectionState,
  stats: DeadLetterStats,
  liveMethodIds: ReadonlySet<string>,
): ChipDisplay {
  if (connection !== "open") {
    return { dotClass: "bg-neutral-400", label: "Runtime: disconnected" };
  }
  const sources = stats.sources;
  if (sources.length === 0) {
    return {
      dotClass: "bg-neutral-400",
      label: "Runtime: off",
      title: "Turn a method on in Setup → Runtime tracing, or set AGENTIC_DEFAULT_TRACE_LOG_PATH before starting the app.",
    };
  }

  const errored = sources.filter((s) => s.state === "error");
  if (errored.length > 0) {
    return {
      dotClass: "bg-red-500",
      label: "Runtime: error",
      title: sources.map(sourceTooltipLine).join("\n"),
    };
  }

  const eventsTotal = sources.reduce((sum, s) => sum + s.eventsReceived, 0);
  const anyLiveRunning = sources.some((s) => s.state === "running" && liveMethodIds.has(s.methodId));
  const label = anyLiveRunning
    ? `Runtime: live · ${eventsTotal} events`
    : `Runtime: on · ${eventsTotal} events`;
  return { dotClass: "bg-emerald-500", label, title: sources.map(sourceTooltipLine).join("\n") };
}

export function RuntimeStatusChip() {
  const connection = useRuntimeStore((s) => s.connection);
  const stats = useRuntimeStore((s) => s.deadLetterStats);
  const [liveMethodIds, setLiveMethodIds] = useState<ReadonlySet<string>>(new Set());

  // Fetched once -- the registry is fixed for the whole backend process
  // lifetime, so there's nothing to re-fetch or subscribe to. Failure is
  // silent and non-fatal: the chip just falls back to "on" wording
  // instead of "live" until it succeeds (or forever, if it never does) --
  // not worth an error state of its own for a cosmetic word choice.
  useEffect(() => {
    let cancelled = false;
    fetchTraceMethods()
      .then((methods) => {
        if (cancelled) return;
        setLiveMethodIds(new Set(methods.filter((m) => m.liveState).map((m) => m.id)));
      })
      .catch(() => {
        // Fallback wording only -- see comment above.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const { dotClass, label, title } = chipDisplay(connection, stats, liveMethodIds);

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-900 dark:border-neutral-700 dark:text-neutral-100"
      title={title}
    >
      <span className={`h-2 w-2 rounded-full ${dotClass}`} aria-hidden="true" />
      {label}
    </span>
  );
}
