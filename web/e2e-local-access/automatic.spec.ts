import { expect, test } from '@playwright/test';
const auth = { Authorization: 'Bearer public-test-fixture-token' };

test.beforeAll(async ({ request }) => {
  await expect.poll(async () => {
    try { return (await request.get('http://127.0.0.1:18009/health/live')).status(); }
    catch { return 0; }
  }, { timeout: 60000 }).toBe(200);
});

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

test('model management saves once after a lost acknowledgement and task sliders submit the selected model', async ({ page, request }) => {
  const saves: Record<string, unknown>[] = [];
  await page.route('**/api/workbench/models', async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    saves.push(route.request().postDataJSON());
    const response = await route.fetch();
    if (saves.length === 1) await route.abort();
    else await route.fulfill({ response });
  });
  await page.goto('/models');
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  await page.getByLabel('配置名称', { exact: true }).fill('Offline reader');
  await page.getByLabel('API 接口地址', { exact: true }).fill('https://models.example/v1');
  await page.getByLabel('模型名称', { exact: true }).fill('synthetic-reader');
  await page.getByLabel('API Key', { exact: true }).fill('public-browser-fixture-model-key');
  await page.getByRole('button', { name: '保存模型', exact: true }).click();
  await page.getByRole('button', { name: '确认 / 重试保存', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('row').filter({ hasText: 'Offline reader' })).toBeVisible();
  expect(saves).toHaveLength(2);
  expect(saves[0]).toEqual(saves[1]);
  expect(await page.locator('body').innerText()).not.toContain('public-browser-fixture-model-key');
  await page.reload();
  await expect(page.getByRole('row').filter({ hasText: 'Offline reader' })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('public-browser-fixture-model-key');

  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: '创建研究任务', exact: true }).click();
  await page.getByLabel('使用模型', { exact: true }).click();
  await page.getByTitle('Offline reader · synthetic-reader', { exact: true }).click();
  await expect(page.getByLabel('使用模型', { exact: true })).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByRole('dialog').getByTitle('Offline reader · synthetic-reader', { exact: true })).toBeVisible();
  await page.getByLabel('研究目标', { exact: true }).fill('Offline model selection acceptance');
  const number = page.getByLabel('总 Token 预算', { exact: true });
  await number.fill('12345');
  const slider = page.getByRole('slider').first();
  await expect(slider).toHaveAttribute('aria-valuenow', '12345');
  await slider.focus(); await page.keyboard.press('ArrowRight');
  await expect(number).toHaveValue('12346');
  await number.fill('10000');
  await page.setViewportSize({ width: 390, height: 844 });
  await number.scrollIntoViewIfNeeded();
  await expect(number).toBeInViewport();
  await expect(slider).toBeInViewport();
  await page.screenshot({ path: test.info().outputPath('model-task-mobile.png') });
  await page.getByLabel('我确认提交后自动执行研究任务，并接受可能产生的 API 费用。').check();
  const submitted = page.waitForRequest((entry) => entry.url().endsWith('/api/workbench/agent/tasks') && entry.method() === 'POST');
  await page.getByRole('button', { name: '确认提交并自动执行', exact: true }).click();
  expect((await submitted).postDataJSON()).toMatchObject({ model_profile_id: saves[0].id, budget: { total_tokens: 10000 } });
  await expect(page).toHaveURL(/\/tasks\//);
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  await expect(page.getByText('Offline reader · synthetic-reader', { exact: true })).toBeVisible();
  const status = await request.get('http://127.0.0.1:18009/__fixture/status', { headers: auth });
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0, task_jobs: 1 });
});
