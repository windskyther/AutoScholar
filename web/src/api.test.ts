import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiError, errorMessage } from './api';

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());

describe('bounded browser API', () => {
  it('uses same-origin bearer reads without cookies or caching', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ items: [], total: 0, limit: 20, offset: 0 }));
    vi.stubGlobal('fetch', fetcher);
    await new ApiClient('test-token').projects(1);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('/api/workbench/projects?limit=20&offset=0');
    expect(options).toMatchObject({ method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error' });
    expect(options.headers.Authorization).toBe('Bearer test-token');
    expect(url).not.toContain('test-token');
  });
  it('does not retry failed requests', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('unavailable'));
    vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('test-token').projects(1)).rejects.toMatchObject({ code: 'network_unavailable' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('clears rejected credentials and calls the disconnection handler', async () => {
    const callback = vi.fn();
    const fetcher = vi.fn().mockResolvedValue(json({ error: { code: 'experiment_auth_required' } }, 401));
    vi.stubGlobal('fetch', fetcher);
    const client = new ApiClient('test-token', callback);
    await expect(client.projects(1)).rejects.toMatchObject({ code: 'experiment_auth_required', status: 401 });
    await expect(client.projects(1)).rejects.toMatchObject({ code: 'experiment_auth_required' });
    expect(callback).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('clear prevents later network access', async () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    const client = new ApiClient('test-token');
    client.clear();
    await expect(client.projects(1)).rejects.toMatchObject({ code: 'experiment_auth_required' });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each(['', 'contains space', '中文', 'a\r\nb', 'a'.repeat(4097)])('rejects invalid header tokens', (token) => {
    expect(() => new ApiClient(token)).toThrow(ApiError);
  });
  it.each(['https://example.org', '//example.org', '/../projects', '/%2e%2e/projects', '/projects/%2fetc'])('rejects unsafe request paths', async (path) => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('test-token').get(path)).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('encodes query text and accepts spaces and slashes in searches', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ items: [], total: 0, limit: 20, offset: 20 }));
    vi.stubGlobal('fetch', fetcher);
    await new ApiClient('test-token').tasks('project', 2, 'paused', 'MNIST 中文 MLP/CNN');
    const params = new URL('http://local' + fetcher.mock.calls[0][0]).searchParams;
    expect(params.get('q')).toBe('MNIST 中文 MLP/CNN');
    expect(params.get('status')).toBe('paused');
    expect(params.get('offset')).toBe('20');
  });
  it.each([null, {}, { status: 'connected' }])('validates the connection response', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(value)));
    await expect(new ApiClient('test-token').session()).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('rejects HTML rather than treating an old API error page as success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<h1>not found</h1>', {
      status: 404, headers: { 'content-type': 'text/html' },
    })));
    await expect(new ApiClient('test-token').projects(1)).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('limits JSON response bytes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json('x'.repeat(4 * 1024 * 1024))));
    await expect(new ApiClient('test-token').projects(1)).rejects.toMatchObject({ code: 'response_too_large' });
  });
  it('decodes UTF-8 across arbitrary byte boundaries', async () => {
    const bytes = new TextEncoder().encode('{"name":"中文项目"}');
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (const value of bytes) controller.enqueue(new Uint8Array([value]));
      controller.close();
    } });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, {
      headers: { 'content-type': 'application/json' },
    })));
    expect(await new ApiClient('test-token').get('/projects')).toEqual({ name: '中文项目' });
  });
  it('shows stable errors without echoing raw upstream messages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: {
      code: 'experiment_api_not_configured', message: 'sensitive-upstream-value',
    } }, 503)));
    try { await new ApiClient('test-token').session(); throw new Error('Expected failure'); }
    catch (error) {
      expect(errorMessage(error)).toContain('EXPERIMENT_API_TOKEN');
      expect(errorMessage(error)).not.toContain('sensitive-upstream-value');
    }
  });
  it.each([
    { items: null, total: 0, limit: 20, offset: 0 },
    { items: [null], total: 1, limit: 20, offset: 0 },
    { items: [], total: -1, limit: 20, offset: 0 },
  ])('rejects malformed list records before rendering', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(value)));
    await expect(new ApiClient('test-token').projects(1)).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('rejects a malformed task overview instead of crashing the page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ task: null, children: {} })));
    await expect(new ApiClient('test-token').overview('task-a', 1)).rejects.toMatchObject({ code: 'invalid_response' });
  });
});
