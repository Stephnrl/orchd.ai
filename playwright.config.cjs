const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30_000,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: 'list',
  // Traces can capture the disposable operator token; keep them disabled.
  use: { browserName: 'chromium', trace: 'off' },
});
