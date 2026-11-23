import { defineConfig, devices } from '@playwright/test';
const baseURL = process.env.PRODUCTION_URL;
if (!baseURL || !/^https?:\/\/(127\.0\.0\.1|localhost):\d+$/.test(baseURL)) {
  throw new Error('Production workflow drill requires owned loopback ingress');
}
if (!process.env.PRODUCTION_TEST_TOKEN) throw new Error('Synthetic fixture token required');
export default defineConfig({
  testDir: './e2e-stack', timeout: 240000, retries: 0, workers: 1,
  reporter: [['list'], ['json', { outputFile: process.env.PRODUCTION_REPORT_FILE }]],
  use: { baseURL, ignoreHTTPSErrors: baseURL.startsWith('https:'), trace: 'off' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
