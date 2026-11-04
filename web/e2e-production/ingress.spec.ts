import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const TOKEN = process.env.PRODUCTION_TEST_TOKEN!;
const headers = { Authorization: 'Bearer ' + TOKEN };
const consent = '我确认将此研究目标交给 Worker 自动执行，并接受可能产生的 API 费用。';
test.describe.configure({ mode: 'serial' });

async function state(request: APIRequestContext) {
  const response = await request.get('/api/__fixture/status', { headers });
  expect(response.status()).toBe(200);
  return response.json() as Promise<{ project_id: string; task_id: string; artifact_id: string; supplier_api_calls: number }>;
}
async function connect(page: Page) {
  const seeded = await state(page.request);
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.locator(`a[href="/projects/${seeded.project_id}"]`).click();
}

test('static SPA, real API errors, private-file denial, host/origin and security headers', async ({ request }) => {
  const spa = await request.get('/projects/a-route-that-has-no-static-file');
  expect(spa.status()).toBe(200);
  expect(await spa.text()).toContain('<div id="root">');
  expect(spa.headers()['x-content-type-options']).toBe('nosniff');
  expect(spa.headers()['content-security-policy']).toContain("connect-src 'self'");
  expect(spa.headers()['referrer-policy']).toBe('no-referrer');
  const absent = await request.get('/api/no-such-route', { headers });
  expect(absent.status()).toBe(404);
  expect(absent.headers()['content-type']).toContain('application/json');
  for (const path of ['/.env', '/.git/config', '/uv.lock', '/src/main.py', '/assets/missing.js']) {
    expect((await request.get(path)).status(), path).toBe(404);
  }
  expect((await request.get('/api/workbench/session')).status()).toBe(401);
  expect((await request.get('/api/workbench/session', { headers: { ...headers, Host: 'untrusted.invalid' } })).status()).toBe(400);
  expect((await request.get('/api/workbench/session', { headers: { ...headers, Origin: 'https://untrusted.invalid', 'X-Forwarded-Proto': 'https' } })).status()).toBe(403);
  expect((await request.get('/api/workbench/session', { headers })).status()).toBe(200);
  const tooLarge = await request.post('/api/workbench/projects', {
    headers: { ...headers, 'Content-Type': 'application/json' },
    data: JSON.stringify({ name: 'x'.repeat(1048576) }),
  });
  expect(tooLarge.status()).toBe(413);
  expect((await request.get('/api/workbench/session', { headers })).status()).toBe(200);
  if (process.env.PRODUCTION_URL!.startsWith('https:')) {
    expect(spa.headers()['strict-transport-security']).toContain('max-age=');
  }
});

test('real PDF upload, asynchronous worker indexing and verified download through ingress', async ({ page, request }) => {
  const seeded = await state(request);
  const pdf = await request.get('/api/__fixture/pdf', { headers });
  const bytes = await pdf.body();
  await connect(page);
  await page.getByRole('button', { name: '上传 PDF', exact: true }).click();
  await page.getByLabel('PDF 文件', { exact: true }).setInputFiles({ name: 'public.pdf', mimeType: 'application/pdf', buffer: bytes });
  await page.getByLabel('文档标题（可选）').fill('生产入口公开 PDF');
  const receipt = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/documents'));
  await page.getByRole('button', { name: '确认上传并索引' }).click();
  expect((await receipt).status()).toBe(202);
  let documentId = '';
  await expect.poll(async () => {
    const response = await request.get(`/api/workbench/projects/${seeded.project_id}/documents`, { headers });
    expect(response.status()).toBe(200);
    const document = (await response.json()).items[0];
    documentId = document.id;
    return document.status;
  }, { timeout: 25000 }).toBe('ready');
  await page.getByRole('button', { name: '刷新文档' }).click();
  await expect(page.getByRole('row').filter({ hasText: '生产入口公开 PDF' }).getByText('已就绪', { exact: true })).toBeVisible();
  const download = await request.get(`/api/workbench/projects/${seeded.project_id}/documents/${documentId}/content`, { headers });
  expect(download.status()).toBe(200);
  expect(download.headers()['content-type']).toContain('application/pdf');
  expect(await download.body()).toEqual(bytes);
});

test('SSE delivers immediately, heartbeats and reconnects after the committed cursor', async ({ page, request }) => {
  const seeded = await state(request);
  await page.goto('/');
  const result = await page.evaluate(async ({ token, task }) => {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 18000);
    const start = performance.now();
    const response = await fetch(`/api/workbench/tasks/${task}/stream`, { headers: { Authorization: 'Bearer ' + token }, signal: controller.signal });
    const reader = response.body!.getReader(); const decoder = new TextDecoder();
    let text = ''; let first = -1;
    try {
      while (!text.includes(': heartbeat')) {
        const item = await reader.read();
        if (first < 0) first = performance.now() - start;
        if (item.done) break;
        text += decoder.decode(item.value, { stream: true });
      }
    } finally { controller.abort(); clearTimeout(deadline); }
    return { text, first, status: response.status, contentType: response.headers.get('content-type') };
  }, { token: TOKEN, task: seeded.task_id });
  expect(result.status).toBe(200);
  expect(result.contentType).toContain('text/event-stream');
  expect(result.first).toBeLessThan(5000);
  expect(result.text).toContain('event: ready');
  expect(result.text).toContain(': heartbeat');
  const cursor = Math.max(...Array.from(result.text.matchAll(/^id: (\d+)/gm), item => Number(item[1])));
  expect(cursor).toBeGreaterThan(0);
  expect((await request.post('/api/__fixture/event', { headers })).status()).toBe(200);
  const next = await page.evaluate(async ({ token, task, cursor }) => {
    const controller = new AbortController(); const deadline = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`/api/workbench/tasks/${task}/stream`, { headers: { Authorization: 'Bearer ' + token, 'Last-Event-ID': String(cursor) }, signal: controller.signal });
      const reader = response.body!.getReader(); let text = ''; const decoder = new TextDecoder();
      while (!/^id: /m.test(text)) { const item = await reader.read(); if (item.done) break; text += decoder.decode(item.value, { stream: true }); }
      return text;
    } finally { controller.abort(); clearTimeout(deadline); }
  }, { token: TOKEN, task: seeded.task_id, cursor });
  expect(Array.from(next.matchAll(/^id: (\d+)/gm), item => Number(item[1]))).toEqual([cursor + 1]);
});

test('Chinese report preview and real authenticated browser export retain bytes and token privacy', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: '公开生产入口验收', exact: true }).click();
  const section = page.getByRole('region', { name: '任务资源' });
  await section.getByRole('tab', { name: '文件与报告' }).click();
  const row = section.getByRole('row').filter({ hasText: 'reports/report.md' });
  await row.getByRole('button', { name: '预览' }).click();
  await expect(page.getByRole('dialog').getByText('# 中文报告', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '关闭预览' }).click();
  const waiting = page.waitForEvent('download');
  await row.getByRole('button', { name: '导出报告' }).click();
  const download = await waiting;
  expect((await readFile((await download.path())!)).toString('utf8')).toContain('# 中文报告');
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie }));
  expect(stored).not.toContain(TOKEN);
  await page.getByRole('button', { name: '断开连接' }).click();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveValue('');
});

test('real task submission retains an idempotent durable receipt with execution held', async ({ page, request }) => {
  await connect(page);
  await page.getByRole('button', { name: '创建研究任务' }).click();
  await page.getByLabel('研究目标', { exact: true }).fill('公开 MNIST 入口提交验收');
  await page.getByRole('checkbox', { name: consent }).check();
  const waiting = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/agent/tasks'));
  await page.getByRole('button', { name: '确认提交并自动执行' }).click();
  const receipt = await waiting;
  expect(receipt.status()).toBe(202);
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const original = receipt.request();
  const replay = await request.post('/api/workbench/agent/tasks', { headers: { ...headers, 'Idempotency-Key': original.headers()['idempotency-key'] }, data: original.postDataJSON() });
  expect(replay.status()).toBe(202);
  expect((await replay.json()).task_id).toBe((await receipt.json()).task_id);
});

test('approval and cancellation use real durable controls without supplier dispatch', async ({ page, request }) => {
  const seeded = await state(request);
  await connect(page);
  await page.getByRole('link', { name: '公开生产入口验收', exact: true }).click();
  const section = page.locator('.control-section');
  await section.getByRole('button', { name: '审阅与决定' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认批准并暂停' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect.poll(async () => {
    const r = await request.get(`/api/workbench/tasks/${seeded.task_id}/controls`, { headers });
    return (await r.json()).status;
  }).toBe('paused');
  await section.getByRole('button', { name: '取消任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认取消任务' }).click();
  await expect(section.getByRole('button', { name: '恢复任务', exact: true })).toBeDisabled();
  expect((await state(request)).supplier_api_calls).toBe(0);
});
