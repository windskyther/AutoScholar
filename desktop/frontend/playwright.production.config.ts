import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.PRODUCTION_URL;
if (!baseURL || !/^https?:\/\/(127\.0\.0\.1|localhost):\d+$/.test(baseURL)) {
  throw new Error('PRODUCTION_URL must name the dedicated loopback acceptance ingress.');
}
if (!process.env.PRODUCTION_TEST_TOKEN) throw new Error('Synthetic acceptance token required.');

export default defineConfig({
  testDir: './e2e-production', timeout: 45000, fullyParallel: false, retries: 0, workers: 1,
  reporter: [['list'], ['json', { outputFile: process.env.PRODUCTION_REPORT_FILE ?? '../data/validation/phase11e-browser-results.json' }]],
  use: { baseURL, ignoreHTTPSErrors: baseURL.startsWith('https:'), trace: 'off' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
