import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { localAccess, operatorCredential } from './local-access.ts';

const root = fileURLToPath(new URL('.', import.meta.url));
const port = process.env.WORKBENCH_API_PORT ?? '8000';
if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
  throw new Error('WORKBENCH_API_PORT must be a valid local port');
}
// Isolated fixture ports retain manual access unless an explicit server-side fixture token is supplied.
const automatic = Number(port) === 8000 || process.env.WORKBENCH_OPERATOR_TOKEN !== undefined;
const access = localAccess(automatic, () => operatorCredential(fileURLToPath(new URL('..', import.meta.url))));
// The upstream host is always loopback. Credentials are added only inside Node.
const proxy = {
  '^/api/workbench(?:/|$)': {
    target: `http://127.0.0.1:${Number(port)}`,
    changeOrigin: false,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
    configure: access.configure,
  },
};

export default defineConfig({
  plugins: [access.plugin, react()],
  // Do not load the repository's .env or expose provider credentials to the browser.
  envDir: root,
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    fs: { strict: true, allow: [root] },
    proxy,
  },
  preview: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
  build: {
    sourcemap: false,
    rolldownOptions: { output: {
      strictExecutionOrder: true,
      codeSplitting: { groups: [{ name: 'react-runtime',
        test: /node_modules[\\/](?:react|react-dom|react-router|react-router-dom|scheduler)[\\/]/, priority: 20 }] },
    } },
  },
  test: { include: ['src/**/*.test.ts'], environment: 'node', restoreMocks: true },
});
