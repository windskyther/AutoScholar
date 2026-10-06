import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const python = fileURLToPath(new URL(process.platform === 'win32'
  ? '../.venv/Scripts/python.exe' : '../.venv/bin/python', import.meta.url));
if (!existsSync(python)) throw new Error('Create the repository .venv with dev dependencies first.');
const fixture = fileURLToPath(new URL('../tests/integration/phase9_workbench_fixture.py', import.meta.url));

export default defineConfig({
  testDir: './e2e-local', timeout: 30000, fullyParallel: false, retries: 0, workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: 'http://127.0.0.1:5173', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    { command: `"${python}" "${fixture}"`, url: 'http://127.0.0.1:18009/__fixture/status',
      timeout: 30000, reuseExistingServer: false },
    { command: 'npm run preview', url: 'http://127.0.0.1:5173', timeout: 30000,
      reuseExistingServer: false, env: { WORKBENCH_API_PORT: '18009' } },
  ],
});
