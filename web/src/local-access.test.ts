import { expect, it, vi } from 'vitest';
import type { IncomingHttpHeaders, IncomingMessage, ServerResponse } from 'node:http';
import type { ProxyOptions } from 'vite';
import { isLocalRequest, localAccess, operatorCredential } from '../local-access';
import { ApiClient } from './api';

function request(headers: IncomingHttpHeaders = {}, remoteAddress = '127.0.0.1'): IncomingMessage {
  return { method: 'GET', url: '/api/local-access', headers: { host: '127.0.0.1:5195', ...headers },
    socket: { remoteAddress, localPort: 5195 } } as unknown as IncomingMessage;
}
function response() {
  const sink = { writeHead: vi.fn(), end: vi.fn() };
  return { sink, http: sink as unknown as ServerResponse };
}

it('reads only the operator credential without exposing provider keys', () => {
  const text = 'LLM_API_KEY=provider-canary\nTAVILY_API_KEY=search-canary\nEXPERIMENT_API_TOKEN="public-server-token"\n';
  expect(operatorCredential('/fixture', {}, () => text)).toBe('public-server-token');
  expect(operatorCredential('/fixture', { WORKBENCH_OPERATOR_TOKEN: 'public-fixture-token' }, () => { throw new Error('must not read real files'); })).toBe('public-fixture-token');
  expect(operatorCredential('/fixture', {}, () => 'LLM_API_KEY=provider-canary')).toBeUndefined();
  expect(operatorCredential('/fixture', { WORKBENCH_OPERATOR_TOKEN: 'bad\r\nheader' })).toBeUndefined();
  expect(operatorCredential('/fixture', {}, () => { throw new Error('private file path'); })).toBeUndefined();
});

it('supports server-side credential files', () => {
  expect(operatorCredential('/fixture', {}, (path) => path.endsWith('.env')
    ? 'EXPERIMENT_API_TOKEN_FILE=internal-token' : 'public-file-token\n')).toBe('public-file-token');
});

it.each([
  { host: 'attacker.example:5195' },
  { host: '127.0.0.1:8000' },
  { host: 'attacker@localhost:5195' },
  { origin: 'https://attacker.example' },
  { origin: 'http://127.0.0.1:5173' },
  { origin: 'null' },
  { 'sec-fetch-site': 'cross-site' },
  { 'sec-fetch-site': 'same-site' },
])('rejects foreign origins, hosts and browser sites %j', (headers) => {
  expect(isLocalRequest(request(headers))).toBe(false);
});

it('uses the actual loopback peer instead of forwarded headers', () => {
  expect(isLocalRequest(request({ 'x-forwarded-for': '127.0.0.1' }, '192.168.1.2'))).toBe(false);
  expect(isLocalRequest(request({ origin: 'http://127.0.0.1:5195', 'sec-fetch-site': 'same-origin' }))).toBe(true);
  expect(isLocalRequest(request())).toBe(true); // local launcher/curl, without browser headers
});

it('bootstrap contains only the access mode, never a credential', () => {
  const access = localAccess(true, () => 'public-server-token'); const { sink, http } = response();
  access.middleware(request(), http, vi.fn());
  expect(sink.writeHead).toHaveBeenCalledWith(200, expect.objectContaining({ 'Cache-Control': 'no-store' }));
  expect(JSON.parse(sink.end.mock.calls[0][0])).toEqual({ mode: 'local_proxy' });
});

it('rejects cross-origin writes and missing configuration before proxying', () => {
  const next = vi.fn(); const denied = response();
  const foreign = request({ origin: 'https://attacker.example' }); foreign.method = 'POST'; foreign.url = '/api/workbench/agent/tasks';
  localAccess(true, () => 'public-server-token').middleware(foreign, denied.http, next);
  expect(denied.sink.writeHead).toHaveBeenCalledWith(403, expect.anything());
  const missing = response(); const local = request(); local.url = '/api/workbench/projects';
  localAccess(true, () => undefined).middleware(local, missing.http, next);
  expect(missing.sink.writeHead).toHaveBeenCalledWith(503, expect.anything());
  expect(next).not.toHaveBeenCalled();
});

it('injects the credential only in the server-side upstream header', () => {
  const access = localAccess(true, () => 'public-server-token'); const on = vi.fn();
  access.configure({ on } as unknown as Parameters<typeof access.configure>[0], {} as ProxyOptions);
  const inject = on.mock.calls[0][1] as (outgoing: unknown, request: IncomingMessage) => void;
  const outgoing = { removeHeader: vi.fn(), setHeader: vi.fn(), destroy: vi.fn() };
  inject(outgoing, request({ authorization: 'Bearer untrusted-browser-value' }));
  expect(outgoing.removeHeader).toHaveBeenCalledWith('authorization');
  expect(outgoing.setHeader).toHaveBeenCalledWith('Authorization', 'Bearer public-server-token');
  outgoing.setHeader.mockClear();
  inject(outgoing, request({ origin: 'https://attacker.example' }));
  expect(outgoing.destroy).toHaveBeenCalledTimes(1); expect(outgoing.setHeader).not.toHaveBeenCalled();
});

it('credentialless local clients send no bearer and cannot be reused after clearing', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }), { headers: { 'content-type': 'application/json' } }));
  vi.stubGlobal('fetch', fetcher);
  try {
    const client = new ApiClient(null); await client.projects(1);
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBeUndefined();
    client.clear(); await expect(client.projects(1)).rejects.toMatchObject({ code: 'experiment_auth_required' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  } finally { vi.unstubAllGlobals(); }
});
