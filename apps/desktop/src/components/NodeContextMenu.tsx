/**
 * Floating context menu that appears at (x, y) when a node is right-clicked.
 *
 * Deliberately minimal for v1 — one item ("Inspect"). The seam is here for
 * future items ("Jump to source", "Copy id", "Hide", "Show only this branch")
 * without touching App.tsx: add a new `<MenuItem>` inline.
 *
 * We render at fixed viewport coordinates, not inside the React Flow canvas —
 * that way the menu stays put during pan/zoom (which would happen mid-click
 * if it were a canvas child) and never gets clipped by the flow container.
 *
 * Closing rules (owned by the parent so multiple triggers stay coherent):
 *   - Any click anywhere else → close.
 *   - Escape → close.
 *   - Selecting an item → close (parent's responsibility inside the handler).
 */

import { useEffect, useRef } from "react";

export interface NodeContextMenuProps {
  x: number;
  y: number;
  nodeName: string;
  onInspect: () => void;
  /** When set, an "Open in IDE" menu item appears. Undefined = the node
   *  has no resolvable code location (workflow, unresolved tool, missing
   *  codebase root) and the item is hidden entirely — a disabled item
   *  invites clicks that go nowhere; hiding it makes the affordance
   *  match the reality. */
  onOpenInIde?: () => void;
  onClose: () => void;
}

export function NodeContextMenu({ x, y, nodeName, onInspect, onOpenInIde, onClose }: NodeContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);

  // Global listeners: outside-click closes the menu, Escape closes it.
  // We attach in `useEffect` so React's own click on this component (which
  // bubbles) doesn't immediately close the menu we just opened. Small trick:
  // register in the *next* tick with a microtask via a flag.
  useEffect(() => {
    let armed = false;
    const arm = () => {
      armed = true;
    };
    // Arm after the mouseup that opened us has fully propagated.
    requestAnimationFrame(arm);

    const onDocClick = (e: MouseEvent) => {
      if (!armed) return;
      if (menuRef.current && menuRef.current.contains(e.target as Node)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // capture: true — React Flow calls stopPropagation on its own
    // mousedown handling for node drag/click. If we listen on the bubble
    // phase, our outside-click never fires when the click landed on the
    // graph canvas, which is exactly where the user usually clicks to
    // dismiss the menu. Capturing at the window level runs BEFORE
    // React Flow's own listener so we always see the click.
    window.addEventListener("mousedown", onDocClick, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDocClick, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // Nudge the menu on-screen if the click was near the right/bottom edges.
  // Cheap heuristic — no width measurement, just cap to viewport.
  const MENU_WIDTH = 200;
  // 76 for the single Inspect row; grow by ~34 per additional row so the
  // clamp keeps the menu on-screen when Open-in-IDE is present too.
  const MENU_HEIGHT = 76 + (onOpenInIde ? 34 : 0);
  const left = Math.min(x, window.innerWidth - MENU_WIDTH - 8);
  const top = Math.min(y, window.innerHeight - MENU_HEIGHT - 8);

  return (
    <div
      ref={menuRef}
      role="menu"
      className="fixed z-50 min-w-[200px] overflow-hidden rounded-md border border-neutral-200 bg-white shadow-lg dark:border-neutral-700 dark:bg-neutral-900 dark:shadow-black/40"
      style={{ left, top }}
    >
      <div
        className="border-b border-neutral-100 px-3 py-1.5 text-xs text-neutral-500 dark:border-neutral-800 dark:text-neutral-400"
        title={nodeName}
      >
        <span className="block overflow-hidden text-ellipsis whitespace-nowrap font-medium text-neutral-700 dark:text-neutral-200">
          {nodeName}
        </span>
      </div>
      <button
        role="menuitem"
        onClick={onInspect}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-neutral-900 hover:bg-neutral-100 dark:text-neutral-100 dark:hover:bg-neutral-800"
      >
        <span>Inspect</span>
        <span className="ml-auto text-xs text-neutral-400 dark:text-neutral-500">details</span>
      </button>
      {onOpenInIde && (
        <button
          role="menuitem"
          onClick={onOpenInIde}
          className="flex w-full items-center gap-2 border-t border-neutral-100 px-3 py-2 text-left text-sm text-neutral-900 hover:bg-neutral-100 dark:border-neutral-800 dark:text-neutral-100 dark:hover:bg-neutral-800"
        >
          <span>Open in IDE</span>
          <span className="ml-auto text-xs text-neutral-400 dark:text-neutral-500">vscode</span>
        </button>
      )}
    </div>
  );
}
