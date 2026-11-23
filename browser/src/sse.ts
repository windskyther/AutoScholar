// UTF-8 SSE framing, intentionally independent of credentials and reconnection policy.
export interface SSEFrame { type: string; id: string | undefined; data: string }
export class StreamProtocolError extends Error {}

export async function consumeSSE(response: Response, receive: (frame: SSEFrame) => void, activity: () => void = () => {}) {
  const reader = response.body?.getReader();
  if (!reader) throw new StreamProtocolError('Missing stream');
  const decoder = new TextDecoder('utf-8', { fatal: true });
  function decode(value?: Uint8Array, stream = false) {
    try { return decoder.decode(value, { stream }); }
    catch { throw new StreamProtocolError('Invalid UTF-8'); }
  }
  let line = ''; let size = 0; let skipLF = false;
  let type = 'message'; let id: string | undefined; let data: string[] = [];
  function finishLine() {
    if (line === '') {
      if (data.length > 0) receive({ type, id, data: data.join('\n') });
      type = 'message'; id = undefined; data = []; size = 0;
    } else if (!line.startsWith(':')) {
      const colon = line.indexOf(':');
      const name = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
      if (name === 'event') type = value;
      else if (name === 'id' && !value.includes('\0')) id = value;
      else if (name === 'data') data.push(value);
    }
    line = '';
  }
  function parse(text: string) {
    for (const char of text) {
      if (skipLF) { skipLF = false; if (char === '\n') continue; }
      size += char.length;
      if (size > 32768) throw new StreamProtocolError('Oversized frame');
      if (char === '\r' || char === '\n') { finishLine(); skipLF = char === '\r'; }
      else line += char;
    }
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      activity(); parse(decode(value, true));
    }
    parse(decode());
    if (line || data.length) throw new StreamProtocolError('Incomplete frame');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
