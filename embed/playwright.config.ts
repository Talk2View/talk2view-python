import { defineConfig, devices } from '@playwright/test';

// The page against the real UI server (e2e/backend.py): build first (`npm run build`).
export default defineConfig({
  testDir: './e2e',
  timeout: 60000,
  expect: { timeout: 15000 },
  fullyParallel: true,
  retries: 0,
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    viewport: { width: 480, height: 800 },
    trace: 'retain-on-failure',
  },
});
