import { afterEach, expect, it, vi } from 'vitest';
import { ApiClient } from './api';

afterEach(() => vi.unstubAllGlobals());

it('downloads full UTF-8 Markdown through authenticated fetch without credentials in the URL', async () => {
  const text = '# 中文输出\n\n\\(y=x^2\\) [E1]\n';
  const fetcher = vi.fn().mockResolvedValue(new Response(text, { headers: { 'content-type': 'text/markdown; charset=utf-8' } }));
  vi.stubGlobal('fetch', fetcher);
  expect(await (await new ApiClient().downloadMarkdown('goal-a')).text()).toBe(text);
  expect(fetcher.mock.calls[0][0]).toBe('/api/workbench/tasks/goal-a/output/markdown');
  expect(fetcher.mock.calls[0][1]).toMatchObject({ headers: { }, credentials: 'omit', redirect: 'error' });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each([401, 403])('Markdown export clears an expired %i session without retry', async (status) => {
  const fetcher = vi.fn().mockResolvedValue(new Response('', { status }));
  vi.stubGlobal('fetch', fetcher);
  const unauthorized = vi.fn(); const api = new ApiClient(unauthorized);
  await expect(api.downloadMarkdown('goal-a')).rejects.toMatchObject({ code: 'local_access_denied' });
  expect(api.connected).toBe(false); expect(unauthorized).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('refuses an HTML response instead of exporting it as Markdown', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>login</html>', { headers: { 'content-type': 'text/html' } })));
  await expect(new ApiClient().downloadMarkdown('goal-a')).rejects.toMatchObject({ code: 'invalid_response' });
});

it('stops an oversized output without creating a truncated export', async () => {
  const bytes = new Uint8Array(8 * 1024 * 1024 + 1);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(bytes, { headers: { 'content-type': 'text/markdown' } })));
  await expect(new ApiClient().downloadMarkdown('goal-a')).rejects.toMatchObject({ code: 'response_too_large' });
});

it('removes exactly the selected project-scoped goal with one authenticated DELETE', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetcher);
  await new ApiClient().deleteTask('project-a', 'goal-a');
  expect(fetcher.mock.calls[0][0]).toBe('/api/workbench/projects/project-a/tasks/goal-a');
  expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'DELETE', headers: { } });
  expect(fetcher).toHaveBeenCalledTimes(1);
});
