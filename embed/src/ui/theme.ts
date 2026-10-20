import type { FontSize, ThemeSlice } from '../wire';

/** Font sizes the Settings view offers (`preferences.fontSize`). */
export const FONT_SIZES: Record<FontSize, string> = { small: '13px', medium: '15px', large: '17px' };

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
