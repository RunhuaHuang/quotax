import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: 'list',
  use: {
    browserName: 'chromium',
    channel: process.env.QUOTAX_TEST_BROWSER_CHANNEL || undefined,
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
