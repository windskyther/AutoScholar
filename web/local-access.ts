import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Plugin, ProxyOptions } from 'vite';

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

export function localAccess(): {
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
    if (!isLocalRequest(request)) { respond(response, 403, { error: { code: 'local_access_denied' } }); return; }
    if (bootstrap) {
      if (request.method !== 'GET') respond(response, 405, { error: { code: 'method_not_allowed' } });
      else respond(response, 200, { mode: 'local_proxy' });
      return;
    }
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
        outgoing.removeHeader('authorization');
        if (!isLocalRequest(request)) { outgoing.destroy(); return; }
      });
    },
  };
}
