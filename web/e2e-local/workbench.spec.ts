import { expect, test } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';

test('browser connects through the real Vite proxy to authenticated FastAPI and SQLite', async ({ page, request }) => {
  const methods: string[] = [];
  page.on('request', (entry) => {
    if (new URL(entry.url()).pathname.startsWith('/api/')) methods.push(entry.method());
  });
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: '中文研究项目' }).click();
  await page.getByRole('link', { name: '比较 MNIST 模型' }).click();
  await expect(page.getByText('中文输出：隔离 HTTP 联测，不调用付费接口。')).toBeVisible();
  await expect(page.getByText('查找公开资料')).toBeVisible();
  await page.getByRole('button', { name: '刷新快照' }).click();
  await expect(page.getByText('已暂停', { exact: true })).toBeVisible();
  const api = await request.get('/api/workbench/session', { headers: { Authorization: 'Bearer ' + TOKEN } });
  expect(api.status()).toBe(200);
  expect(api.headers()['cache-control']).toBe('private, no-store');
  const unauthenticated = await request.get('/api/workbench/projects');
  expect(unauthenticated.status()).toBe(401);
  const state = await request.get('http://127.0.0.1:18009/__fixture/status');
  expect(await state.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0 });
  expect((await state.json()).api_requests).toBeGreaterThanOrEqual(7);
  expect(methods.length).toBeGreaterThanOrEqual(5);
  expect(methods.every((method) => method === 'GET')).toBeTruthy();
});

test('wrong token fails against the actual API and fixture refuses writes', async ({ page, request }) => {
  await page.goto('/connect');
  await page.getByLabel('操作者 Token', { exact: true }).fill('wrong-fixture-token');
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await expect(page.getByText('操作者 Token 无效，请重新连接。')).toBeVisible();
  const blocked = await request.post('/api/workbench/projects', {
    headers: { Authorization: 'Bearer ' + TOKEN }, data: { name: 'must not create' },
  });
  expect(blocked.status()).toBe(405);
});
