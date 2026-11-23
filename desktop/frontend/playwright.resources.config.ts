import { defineConfig } from '@playwright/test';
import base from './playwright.local.config';
const servers = base.webServer;
if (!Array.isArray(servers)) throw new Error('Local fixture server configuration missing');
export default defineConfig({ ...base, testDir: './e2e-resources-local',
  webServer: [{ ...servers[0], command: servers[0].command + ' --resources', stdout: 'pipe' }, servers[1]],
});
