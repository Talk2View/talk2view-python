import type { FontSize, ThemeSlice } from '../wire';

/**
 * Font sizes the Settings view offers (`preferences.fontSize`).
 * Medium is now TS's own 16px base (thread.aui.tsx:539's un-classed text, the browser
 * default) rather than a smaller 15px — the reply (`.aui-assistant-message-content`,
 * app.css) inherits this instead of a fixed literal, so Text size now reaches the one
 * text it exists for, and the user bubble moves with it too.
 */
export const FONT_SIZES: Record<FontSize, string> = { small: '14px', medium: '16px', large: '18px' };

let applied: string[] = [];

/**
 * Put the host's theme on the page: `mode` as `data-theme` on <html> (the stylesheet
 * carries the dark palette), each `--t2v-*` token as an inline custom property (the host
 * wins over the stylesheet), and the end-user's font size. Tokens the host dropped go.
 */
export function applyTheme(theme: ThemeSlice, fontSize: FontSize | undefined): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', theme.mode === 'dark' ? 'dark' : 'light');
  for (const name of applied) if (!(name in theme.tokens)) root.style.removeProperty(name);
  applied = [];
  for (const [name, value] of Object.entries(theme.tokens)) {
    if (!/^--t2v-[a-z0-9-]{1,64}$/.test(name)) continue; // the server checks it too
    root.style.setProperty(name, value);
    applied.push(name);
  }
  root.style.setProperty('--t2v-font-size', FONT_SIZES[fontSize ?? 'medium']);
}
