import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './e2e-stack', timeout: 180000, fullyParallel: false, workers: 1, retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: 'http://127.0.0.1:5183', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: { command: 'npm run preview -- --port 5183', url: 'http://127.0.0.1:5183', reuseExistingServer: false,
    env: { WORKBENCH_API_PORT: '18019' } },
});
