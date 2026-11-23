import { defineConfig, devices } from '@playwright/test';
const webPort = process.env.STACK_WEB_PORT ?? '5183';
const apiPort = process.env.STACK_API_PORT ?? '18019';
for (const port of [webPort, apiPort]) {
  if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) throw new Error('Invalid local acceptance port');
}
export default defineConfig({
  testDir: './e2e-stack', timeout: 180000, fullyParallel: false, workers: 1, retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: `http://127.0.0.1:${webPort}`, trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: { command: `npm run preview -- --port ${webPort}`, url: `http://127.0.0.1:${webPort}`, reuseExistingServer: false,
    env: { WORKBENCH_API_PORT: apiPort } },
});
