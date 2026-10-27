/**
 * Shared keyboard-shortcut guard: is the user currently typing into a text
 * field? A global shortcut (like `/` for search) must not fire while the
 * user is already typing somewhere — otherwise `/` would either get eaten
 * mid-sentence or double-fire the shortcut. Shared (not duplicated) so
 * App.tsx's `/`-to-search shortcut and any future single-key shortcut use
 * the same definition of "a field" instead of two that can drift apart.
 */
export function isTypingInField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  // `!!` guards against `isContentEditable` being `undefined` rather than
  // `false` on an ordinary element in some DOM implementations (observed in
  // jsdom) — without it, `false || false || undefined` evaluates to
  // `undefined`, not `false`, breaking strict `=== false` callers.
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    !!target.isContentEditable
  );
}
