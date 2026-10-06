import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Keep downloaded browsers off C: and out of Git; no global Node configuration changes.
process.env.PLAYWRIGHT_BROWSERS_PATH ??= fileURLToPath(new URL('../../data/tooling/playwright', import.meta.url));
const cli = fileURLToPath(new URL('../node_modules/@playwright/test/cli.js', import.meta.url));
const result = spawnSync(process.execPath, [cli, ...process.argv.slice(2)], {
  env: process.env, stdio: 'inherit', windowsHide: true,
});
if (result.error) { process.stderr.write('Could not start Playwright.\n'); process.exitCode = 1; }
else process.exitCode = result.status ?? 1;
