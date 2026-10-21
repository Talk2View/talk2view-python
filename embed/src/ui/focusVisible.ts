/**
 * A `:focus-visible` stand-in for Chromium 83 (no native support until 86). Without it,
 * app.css's outline rules draw on any `:focus` — a mouse click included — which is all a
 * plain `:focus` selector can tell it: a Teal (or, in dark mode, Highlight) box left on a
 * tool row after it was clicked, not tabbed to. TS avoids this with the real
 * `focus-visible:ring-ring/50` Tailwind's own pointer-vs-keyboard heuristic.
 *
 * This reimplements that heuristic the way the WICG focus-visible polyfill does, since
 * the CSS pseudo-class itself is out of reach: track the last input the page saw, at the
 * `document` level so it never has to know about any one focusable element, and toggle
 * one class the stylesheet keys off. A pointer press means the very next focus is a click,
 * not a Tab, so it suppresses the ring; a keypress means a keyboard user is now driving
 * (Tab is the common case, but any key is treated the same, matching the polyfill's own
 * choice of "assume keyboard until a pointer press proves otherwise" over trying to
 * recognise Tab specifically) — except a keydown held with Meta, Alt or Ctrl, which the
 * WICG polyfill's own `onKeyDown` also excludes: a mouse user who
 * clicks a button and then presses Ctrl+C to copy selected reply text is not "now driving
 * by keyboard" in the sense this heuristic means, and native Chromium's own
 * `:focus-visible` ignores a modifier-only or modifier-held press the same way. Both
 * listeners run in the capture phase, before the focus event itself, so the element that
 * ends up focused already has the right class the first time it paints.
 */
export function installFocusVisible(): void {
  const root = document.documentElement;
  const onPointer = () => root.classList.add('t2v-mouse');
  const onKey = (e: KeyboardEvent) => {
    if (e.metaKey || e.altKey || e.ctrlKey) return;
    root.classList.remove('t2v-mouse');
  };
  document.addEventListener('pointerdown', onPointer, true);
  document.addEventListener('keydown', onKey, true);
}
