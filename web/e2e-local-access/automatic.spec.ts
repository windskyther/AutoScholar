import { expect, test } from '@playwright/test';
const auth = { Authorization: 'Bearer public-test-fixture-token' };

test('local proxy authenticates server-side while browser opens and refreshes without a login', async ({ page, request }) => {
  const headers: Record<string, string>[] = [];
  page.on('request', (entry) => { if (entry.url().includes('/api/')) headers.push(entry.headers()); });
  const direct = await request.get('http://127.0.0.1:18009/workbench/session');
  expect(direct.status()).toBe(401);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  expect(headers.length).toBeGreaterThan(0);
  expect(headers.every((item) => !item.authorization && !item.cookie)).toBe(true);
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie }));
  expect(stored).not.toContain('public-test-fixture-token');
});

test('automatic local access permits a same-origin project write without a browser credential', async ({ page, request }) => {
  const headers: Record<string, string>[] = [];
  page.on('request', (entry) => {
    if (entry.url().includes('/api/workbench/')) headers.push(entry.headers());
  });
  await page.goto('/');
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill('Automatic access acceptance');
  await page.getByLabel('项目说明', { exact: true }).fill('Synthetic local fixture; no provider calls.');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Automatic access acceptance', exact: true })).toBeVisible();
  expect(headers.length).toBeGreaterThan(0);
  expect(headers.every((item) => !item.authorization && !item.cookie)).toBe(true);
  const status = await request.get('http://127.0.0.1:18009/__fixture/status', { headers: auth });
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0, task_jobs: 0 });
});

test('local automatic access rejects foreign origins and leaves providers untouched', async ({ request }) => {
  const refused = await request.post('/api/workbench/agent/tasks', {
    headers: { Origin: 'https://untrusted.example' }, data: { objective: 'must not execute' },
  });
  expect(refused.status()).toBe(403);
  const blocked = await request.get('/api/workbench/projects', { headers: { 'Sec-Fetch-Site': 'cross-site' } });
  expect(blocked.status()).toBe(403);
  const status = await request.get('http://127.0.0.1:18009/__fixture/status', { headers: auth });
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0 });
});
