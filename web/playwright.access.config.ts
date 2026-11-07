import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const python = fileURLToPath(new URL(process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python', import.meta.url));
const fixture = fileURLToPath(new URL('../tests/integration/phase9_workbench_fixture.py', import.meta.url));
export default defineConfig({
  testDir: './e2e-local-access', timeout: 30000, retries: 0, workers: 1,
  reporter: [['list']], use: { baseURL: 'http://127.0.0.1:5196', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    { command: `"${python}" "${fixture}" --writes`, url: 'http://127.0.0.1:18009/health/live', timeout: 60000, reuseExistingServer: false },
    { command: 'npm run preview -- --port 5196', url: 'http://127.0.0.1:5196', timeout: 30000, reuseExistingServer: false,
      env: { WORKBENCH_API_PORT: '18009' } },
  ],
});
