import { readFileSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';
import type { Plugin, ProxyOptions } from 'vite';

// This module runs in the local Node server, never in the browser bundle.
export function operatorCredential(root: string, environment: NodeJS.ProcessEnv = process.env,
  read: (path: string) => string = (path) => readFileSync(path, 'utf8')): string | undefined {
  const explicit = environment.WORKBENCH_OPERATOR_TOKEN;
  if (explicit !== undefined) return /^[\x21-\x7e]{1,4096}$/.test(explicit) ? explicit : undefined;
  let local: Record<string, string | undefined> = {};
  try { local = parseEnv(read(resolve(root, '.env'))); } catch { /* Missing configuration is reported without its contents. */ }
  const file = environment.EXPERIMENT_API_TOKEN_FILE ?? local.EXPERIMENT_API_TOKEN_FILE;
  let token = environment.EXPERIMENT_API_TOKEN ?? local.EXPERIMENT_API_TOKEN;
  if (file) { try { token = read(resolve(root, file)).trim(); } catch { return undefined; } }
  return token && /^[\x21-\x7e]{1,4096}$/.test(token) ? token : undefined;
}

export function isLocalRequest(request: IncomingMessage): boolean {
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(request.socket.remoteAddress ?? '')) return false;
  try {
    const target = new URL('http://' + request.headers.host);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname) || target.username || target.password
      || target.pathname !== '/' || target.search || target.hash
      || Number(target.port || 80) !== request.socket.localPort) return false;
    const origin = request.headers.origin;
    const site = request.headers['sec-fetch-site'];
    return (!origin || origin === target.origin) && (!site || site === 'same-origin' || site === 'none');
  } catch { return false; }
}

export function localAccess(enabled: boolean, credential: () => string | undefined): {
  plugin: Plugin; configure: NonNullable<ProxyOptions['configure']>;
  middleware: (request: IncomingMessage, response: ServerResponse, next: () => void) => void;
} {
  function respond(response: ServerResponse, status: number, payload: unknown) {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify(payload));
  }
  function middleware(request: IncomingMessage, response: ServerResponse, next: () => void) {
    const path = request.url?.split('?')[0];
    const bootstrap = path === '/api/local-access';
    if (!bootstrap && !/^\/api\/workbench(?:\/|$)/.test(path ?? '')) { next(); return; }
    if (!enabled) { if (bootstrap) respond(response, 404, { error: { code: 'not_found' } }); else next(); return; }
    if (!isLocalRequest(request)) { respond(response, 403, { error: { code: 'local_access_denied' } }); return; }
    if (bootstrap) {
      if (request.method !== 'GET') respond(response, 405, { error: { code: 'method_not_allowed' } });
      else respond(response, 200, { mode: 'local_proxy' });
      return;
    }
    if (!credential()) { respond(response, 503, { error: { code: 'local_proxy_not_configured' } }); return; }
    next();
  }
  return {
    middleware,
    plugin: {
      name: 'autoscholar-local-access',
      configureServer(server) { server.middlewares.use(middleware); },
      configurePreviewServer(server) { server.middlewares.use(middleware); },
    },
    configure(proxy) {
      proxy.on('proxyReq', (outgoing, request) => {
        if (!enabled) return;
        outgoing.removeHeader('authorization');
        const token = credential();
        if (!isLocalRequest(request) || !token) { outgoing.destroy(); return; }
        outgoing.setHeader('Authorization', 'Bearer ' + token);
      });
    },
  };
}
