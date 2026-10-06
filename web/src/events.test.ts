import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './api';
import { consumeSSE, StreamProtocolError } from './sse';
import type { StreamMessage } from './types';

const task = 'task-a';
const event = { task_id: task, sequence: 2, kind: 'checkpoint_saved', payload: { stage: 'writer', status: 'succeeded' }, created_at: '2026-10-06T00:00:00Z' };
const ready = 'event: ready\ndata: ' + JSON.stringify({ task_id: task, status: 'running', durable: true }) + '\n\n';
const end = 'event: end\ndata: ' + JSON.stringify({ task_id: task, status: 'succeeded', reason: 'terminal' }) + '\n\n';
const workflow = 'id: 2\nevent: workflow\ndata: ' + JSON.stringify(event) + '\n\n';

function response(text: string, chunkSize = 4096) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    for (let index = 0; index < bytes.length; index += chunkSize) controller.enqueue(bytes.slice(index, index + chunkSize));
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } });
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('bounded UTF-8 event framing', () => {
  it.each(['\n', '\r\n', '\r'])('handles split Chinese, comments, multiline data and %j framing', async (newline) => {
    const received: unknown[] = [];
    const text = '\uFEFF: heartbeat\n\nevent: workflow\nid: 7\ndata: 中文\ndata: 第二行\n\nretry: 0\n\n'.replaceAll('\n', newline);
    await consumeSSE(response(text, 1), (frame) => received.push(frame));
    expect(received).toEqual([{ type: 'workflow', id: '7', data: '中文\n第二行' }]);
  });
  it('ignores event-only frames, extra fields and heartbeat comments', async () => {
    const received: unknown[] = [];
    await consumeSSE(response('event: ignored\n\n: heartbeat\n\nextra: noop\ndata: {}\n\n'), (frame) => received.push(frame));
    expect(received).toEqual([{ type: 'message', id: undefined, data: '{}' }]);
  });
  it.each(['data: ' + 'x'.repeat(32769) + '\n\n', ': ' + 'x'.repeat(32769) + '\n\n'])('bounds data and comment frames', async (text) => {
    await expect(consumeSSE(response(text), () => {})).rejects.toBeInstanceOf(StreamProtocolError);
  });
  it.each(['data: {}\n', 'event: workflow\ndata: {'])('never dispatches a truncated frame at EOF', async (text) => {
    const receive = vi.fn();
    await expect(consumeSSE(response(text), receive)).rejects.toBeInstanceOf(StreamProtocolError);
    expect(receive).not.toHaveBeenCalled();
  });
  it('rejects malformed UTF-8 rather than replacing Chinese characters', async () => {
    const bad = new Response(new Uint8Array([0xff]), { headers: { 'Content-Type': 'text/event-stream' } });
    await expect(consumeSSE(bad, () => {})).rejects.toBeInstanceOf(StreamProtocolError);
  });
});

describe('authenticated read-only task streams', () => {
  it('sends Bearer and cursor only in headers, parses safe frames and does not submit a task', async () => {
    const fetcher = vi.fn().mockResolvedValue(response(ready + workflow + end, 1));
    vi.stubGlobal('fetch', fetcher);
    const messages: StreamMessage[] = [];
    await new ApiClient('public-test-token').stream(task, 1, new AbortController().signal, (message) => messages.push(message));
    expect(messages).toEqual([{ type: 'ready', status: 'running', durable: true }, { type: 'workflow', event }, { type: 'end', status: 'succeeded', reason: 'terminal' }]);
    expect(fetcher.mock.calls[0][0]).toBe('/api/workbench/tasks/task-a/stream');
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: { Authorization: 'Bearer public-test-token', 'Last-Event-ID': '1', Accept: 'text/event-stream' } });
    expect(fetcher.mock.calls[0][1].body).toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it.each([401, 403])('clears credentials on HTTP %i without retrying', async (status) => {
    const unauthorized = vi.fn(); const fetcher = vi.fn().mockResolvedValue(new Response(null, { status })); vi.stubGlobal('fetch', fetcher);
    const api = new ApiClient('public-test-token', unauthorized);
    await expect(api.stream(task, 0, new AbortController().signal, () => {})).rejects.toMatchObject({ name: 'AbortError' });
    expect(api.connected).toBe(false); expect(unauthorized).toHaveBeenCalledTimes(1); expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it.each([ready + 'id: 9\nevent: workflow\ndata: ' + JSON.stringify(event) + '\n\n',
    ready + 'id: 2\nevent: workflow\ndata: ' + JSON.stringify({ ...event, task_id: 'foreign' }) + '\n\n',
    ready + 'id: 2\nevent: workflow\ndata: ' + JSON.stringify({ ...event, payload: { secret: 'PRIVATE' } }) + '\n\n',
    ready + 'event: bad\ndata: {}\n\n', ready + 'event: end\ndata: null\n\n', ready + end + workflow,
    ready + 'event: end\ndata: {"task_id":"task-a","status":"running","reason":"terminal"}\n\n',
    ready + 'event: end\ndata: {"task_id":"task-a","status":"succeeded","reason":"rotate"}\n\n',
    ready + 'event: end\ndata: {"task_id":"task-a","status":"running","reason":"unsupported"}\n\n',
  ])('stops on invalid/mismatched/unsafe frames without retrying', async (text) => {
    const fetcher = vi.fn().mockResolvedValue(response(text)); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('public-test-token').stream(task, 1, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('treats a browser fetch network TypeError as a reconnectable read failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(new ApiClient('public-test-token').stream(task, 0, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'network_unavailable' });
  });
  it('preserves backend cursor errors and retryable in-stream storage failure', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'event_cursor_invalid' } }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(response(ready + 'event: error\ndata: {"code":"workbench_stream_unavailable"}\n\n'));
    vi.stubGlobal('fetch', fetcher);
    const api = new ApiClient('public-test-token');
    await expect(api.stream(task, 5, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'event_cursor_invalid', status: 409 });
    await expect(api.stream(task, 1, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'workbench_stream_unavailable', status: 503 });
  });
  it('aborts active bodies and prevents further requests when disconnected', async () => {
    let writer: ReadableStreamDefaultController<Uint8Array>;
    const fetcher = vi.fn().mockImplementation(async (_: string, options: RequestInit) => {
      const body = new ReadableStream<Uint8Array>({ start(value) { writer = value; writer.enqueue(new TextEncoder().encode(ready)); } });
      options.signal?.addEventListener('abort', () => writer.error(new DOMException('Aborted', 'AbortError')), { once: true });
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
    });
    vi.stubGlobal('fetch', fetcher);
    const api = new ApiClient('public-test-token'); const observed = vi.fn();
    const pending = api.stream(task, 0, new AbortController().signal, observed);
    await vi.waitFor(() => expect(observed).toHaveBeenCalled());
    api.clear();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await expect(api.stream(task, 0, new AbortController().signal, observed)).rejects.toMatchObject({ code: 'experiment_auth_required' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('bounds silent connections with a body idle timeout', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_: string, options: RequestInit) => {
      const body = new ReadableStream<Uint8Array>({ start(writer) {
        options.signal?.addEventListener('abort', () => writer.error(new DOMException('Aborted', 'AbortError')), { once: true });
      } });
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
    }));
    const pending = new ApiClient('public-test-token').stream(task, 0, new AbortController().signal, () => {});
    const rejected = expect(pending).rejects.toMatchObject({ code: 'network_unavailable' });
    await vi.advanceTimersByTimeAsync(35001); await rejected;
  });
  it('rejects invalid history identity and unsafe cursors locally', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ task_id: 'foreign', status: 'running', durable: true, items: [], next_cursor: 0, has_more: false, has_older: false }), { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetcher); const api = new ApiClient('public-test-token');
    await expect(api.events(task)).rejects.toMatchObject({ code: 'invalid_response' });
    await expect(api.stream(task, -1, new AbortController().signal, () => {})).rejects.toMatchObject({ code: 'event_cursor_invalid' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
