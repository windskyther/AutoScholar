import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', timeout: 30000, fullyParallel: false, retries: 0, workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: 'http://127.0.0.1:5173', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev', url: 'http://127.0.0.1:5173', timeout: 30000,
    reuseExistingServer: false,
  },
});
