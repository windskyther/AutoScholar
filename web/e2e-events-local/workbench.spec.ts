import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const fixtureURL = 'http://127.0.0.1:18009';
const headers = { Authorization: 'Bearer ' + TOKEN };
test.describe.configure({ mode: 'serial' });
async function connect(page: Page) {
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: '中文研究项目' }).click();
  await page.getByRole('link', { name: '公开的实时事件验收' }).click();
}
async function advance(request: APIRequestContext, finish = false): Promise<number> {
  const receipt = await request.post(fixtureURL + '/__fixture/advance?finish=' + finish, { headers });
  expect(receipt.status()).toBe(200); return (await receipt.json()).sequence;
}
test.afterAll(async ({ request }) => {
  const status = await request.get(fixtureURL + '/__fixture/status');
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0, task_jobs: 1 });
  expect((await request.post(fixtureURL + '/__fixture/stop', { headers })).status()).toBe(200);
  await expect.poll(async () => {
    try { await request.get(fixtureURL + '/__fixture/status', { timeout: 1000 }); return false; }
    catch { return true; }
  }, { timeout: 10000 }).toBe(true);
});

test('real authenticated SSE updates snapshot and paged history through the Vite production proxy', async ({ page, request }) => {
  const methods: string[] = [];
  page.on('request', (entry) => { if (new URL(entry.url()).pathname.startsWith('/api/')) methods.push(entry.method()); });
  await connect(page);
  await expect(page.getByText('事件流已连接', { exact: true })).toBeVisible();
  const section = page.locator('.event-section');
  await section.getByRole('button', { name: '更早记录' }).click();
  await expect(section.getByText('正在浏览历史页，最新事件仍在后台接收。')).toBeVisible();
  await expect(section.getByText('#55', { exact: true })).toBeVisible();
  await section.getByRole('button', { name: '返回最新' }).click();
  const receipt = await advance(request);
  await expect(page.locator('.metric').filter({ hasText: '总 Tokens' }).locator('strong')).toHaveText('24');
  await expect(page.getByText('writer', { exact: true }).first()).toBeVisible();
  const current = await request.get('/api/workbench/tasks/' + new URL(page.url()).pathname.split('/').at(-1) + '/events', { headers });
  expect((await current.json()).next_cursor).toBe(receipt);
  expect(await current.text()).not.toContain('PRIVATE_FIXTURE_PARAMETER');
  expect(methods.every((method) => method === 'GET')).toBe(true);
  await page.screenshot({ path: 'test-results/phase9d-events.png', fullPage: true });
});

test('a cut real stream resumes after its last delivered sequence and terminates without rerunning work', async ({ page, request }) => {
  let streams = 0; let delivered = 0; let initialCursor: string | undefined;
  await page.route('**/api/workbench/tasks/*/stream', async (route) => {
    streams += 1;
    expect(route.request().headers().authorization).toBe('Bearer ' + TOKEN);
    if (streams === 1) {
      initialCursor = route.request().headers()['last-event-id'];
      delivered = await advance(request);
      const response = await route.fetch({ timeout: 15000, maxRetries: 0, maxRedirects: 0 });
      const body = (await response.text()).split('event: end\n')[0];
      expect(body).toContain('id: ' + delivered + '\n');
      await route.fulfill({ response, body });
    } else {
      expect(route.request().headers()['last-event-id']).toBe(String(delivered));
      await advance(request, true);
      await route.continue();
    }
  });
  await connect(page);
  await expect(page.getByText('任务已结束，事件已同步', { exact: true })).toBeVisible({ timeout: 15000 });
  expect(Number(initialCursor)).toBe(delivered - 1);
  expect(streams).toBe(2);
  await expect(page.getByText('中文事件验收完成：仅夹具状态变化，没有执行模型。')).toBeVisible();
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookies: window.document.cookie }));
  expect(stored).not.toContain(TOKEN); expect(page.url()).not.toContain(TOKEN);
});

test('actual streaming auth failure disconnects and clears cached task pages', async ({ page }) => {
  let attempts = 0;
  await page.route('**/api/workbench/tasks/*/stream', async (route) => {
    attempts += 1;
    const response = await route.fetch({ headers: { ...route.request().headers(), authorization: 'Bearer wrong-public-fixture-token' }, maxRetries: 0, maxRedirects: 0 });
    expect(response.status()).toBe(401); await route.fulfill({ response });
  });
  await connect(page);
  await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toHaveCount(0);
  expect(attempts).toBe(1);
});
