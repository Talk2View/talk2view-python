import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

/** Where `npm run build` puts the page: inside the Python package (static.BUILD_INSTRUCTIONS). */
export const STATIC_DIR = here('../src/talk2view/ui/static');

// Everything here exists to make the output loadable by Chromium 83 (Qt 5.15.2
// WebEngine in official 3D Slicer builds on Windows and Linux).
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  // Relative asset URLs: UIServer serves the bundle from "/", a wheel from anywhere.
  base: './',
  resolve: {
    alias: {
      // The TS SDK files the page imports (convert.ts, types.ts, safeMarkdown.ts,
      // constants.ts), vendored from the public talk2view-typescript repository: the page
      // never reads a checkout of it (README.md, "Where the TS SDK code comes from").
      '@sdk': here('./src/vendor/sdk'),
      // The runtime guard's self-test: a no-op in the shipped page.
      '#guard': here(mode === 'guard' ? './src/guard/index.ts' : './src/guard/noop.ts'),
    },
  },
  build: {
    target: 'chrome83',
    cssTarget: 'chrome83',
    outDir: mode === 'guard' ? here('./dist-guard') : STATIC_DIR,
    emptyOutDir: true,
    cssCodeSplit: false,
    // A data: font would break the CSP (font-src falls back to 'self').
    assetsInlineLimit: 0,
    // The preload polyfill is an inline script, which the CSP blocks.
    modulePreload: { polyfill: false },
    sourcemap: false,
    chunkSizeWarningLimit: 1024,
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
}));
