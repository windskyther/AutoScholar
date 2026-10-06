import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const base = 'http://127.0.0.1:18009';
const headers = { Authorization: 'Bearer ' + TOKEN };
test.describe.configure({ mode: 'serial' });
async function connect(page: Page, request: APIRequestContext): Promise<string> {
  const seeded = await request.post(base + '/__fixture/control-seed', { headers }); expect(seeded.status()).toBe(200);
  const id: string = (await seeded.json()).task_id;
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: '中文研究项目' }).click();
  await page.locator(`a[href="/projects/project-a/tasks/${id}"]`).click();
  await expect(page.getByRole('heading', { name: '任务控制与审批' })).toBeVisible();
  return id;
}
async function state(request: APIRequestContext, id: string) {
  const response = await request.get(base + `/workbench/tasks/${id}/controls`, { headers }); expect(response.status()).toBe(200);
  return response.json();
}
test.afterAll(async ({ request }) => {
  const response = await request.get(base + '/__fixture/status');
  expect(await response.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0 });
  expect((await request.post(base + '/__fixture/stop', { headers })).status()).toBe(200);
  await expect.poll(async () => {
    try { await request.get(base + '/__fixture/status', { timeout: 1000 }); return false; } catch { return true; }
  }, { timeout: 10000 }).toBe(true);
});

test('real approval stays paused, explicit resume/pause/cancel require confirmation and never run providers', async ({ page, request }) => {
  const writes: string[] = [];
  page.on('request', (entry) => { if (entry.method() === 'POST' && new URL(entry.url()).pathname.startsWith('/api/')) writes.push(new URL(entry.url()).pathname); });
  const id = await connect(page, request);
  const section = page.locator('.control-section');
  await expect(section.getByRole('button', { name: '恢复任务', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: '审阅与决定' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('风险级别 3 · MNIST CPU 实验')).toBeVisible();
  await dialog.locator('summary').click();
  await expect(dialog.locator('pre')).toContainText('train_samples');
  await dialog.locator('summary').click();
  await page.screenshot({ path: 'test-results/controls-review-desktop.png' });
  await dialog.getByRole('button', { name: '确认批准并暂停' }).click();
  await expect(dialog).toHaveCount(0);
  expect((await state(request, id)).status).toBe('paused');
  expect(writes).toHaveLength(1);
  await section.getByRole('button', { name: '恢复任务', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('恢复后可能产生 LLM、检索等 API 费用及本地训练记录。')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭并刷新' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(writes).toHaveLength(1);
  await section.getByRole('button', { name: '恢复任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认恢复任务' }).click();
  await expect.poll(async () => (await state(request, id)).status).toBe('queued');
  await expect(section.getByRole('button', { name: '暂停任务', exact: true })).toBeEnabled();
  await section.getByRole('button', { name: '暂停任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认暂停任务' }).click();
  await expect.poll(async () => (await state(request, id)).status).toBe('paused');
  await section.getByRole('button', { name: '取消任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '关闭并刷新' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await state(request, id)).status).toBe('paused');
  await section.getByRole('button', { name: '取消任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认取消任务' }).click();
  await expect.poll(async () => (await state(request, id)).status).toBe('cancelled');
  await expect(section.getByRole('button', { name: '恢复任务', exact: true })).toBeDisabled();
  expect(writes).toHaveLength(4);
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie }));
  expect(stored).not.toContain(TOKEN); expect(page.url()).not.toContain(TOKEN);
});

test('real reject then parameter modification creates version 2 without training, including mobile review', async ({ page, request }) => {
  const id = await connect(page, request);
  await page.setViewportSize({ width: 390, height: 844 });
  const section = page.locator('.control-section');
  await section.getByRole('button', { name: '审阅与决定' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '拒绝', exact: true }).click();
  await page.getByRole('dialog').getByLabel('决定理由（可选，不要填写密钥）').fill('减少训练轮数');
  await page.getByRole('dialog').getByRole('button', { name: '确认拒绝', exact: true }).click();
  await expect(section.getByText('已拒绝', { exact: true })).toBeVisible();
  expect((await state(request, id)).status).toBe('awaiting_approval');
  await section.getByRole('button', { name: '审阅与决定' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '修改参数', exact: true }).click();
  await page.getByRole('dialog').getByLabel('训练轮数', { exact: true }).fill('1');
  await page.screenshot({ path: 'test-results/controls-modify-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('dialog').getByRole('button', { name: '确认修改并暂停' }).click();
  await expect.poll(async () => (await state(request, id)).status).toBe('paused');
  const overview = await request.get(base + `/workbench/tasks/${id}/overview`, { headers });
  expect((await overview.json()).current_plan.version).toBe(2);
  await expect(section.getByText('已被新参数取代', { exact: true })).toBeVisible();
  await section.getByRole('button', { name: '查看审批详情' }).click();
  await expect(page.getByRole('dialog').getByRole('button', { name: '仅查看' })).toBeDisabled();
});

test('another client changes task while modal is open: stale submission is blocked', async ({ page, request }) => {
  const id = await connect(page, request);
  await page.locator('.control-section').getByRole('button', { name: '审阅与决定' }).click();
  const prior = await state(request, id);
  const cancel = await request.post(base + `/workbench/tasks/${id}/control/cancel`, { headers, data: { expected: prior.expected } });
  expect(cancel.status()).toBe(200);
  await expect(page.getByRole('dialog').getByText('状态或有效期已变化，请关闭窗口并刷新后重新审阅。')).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('dialog').getByRole('button', { name: '确认批准并暂停' })).toBeDisabled();
  const stale = await request.post(base + `/workbench/tasks/${id}/control/cancel`, { headers, data: { expected: prior.expected } });
  expect(stale.status()).toBe(409);
});

test('lost successful approval receipt locks writes and manual read-only inspection does not repeat decision', async ({ page, request }) => {
  const id = await connect(page, request); let decisions = 0;
  await page.route('**/api/workbench/tasks/*/approvals/*/decision', async (route) => {
    decisions += 1; const response = await route.fetch(); expect(response.status()).toBe(200); await route.abort('failed');
  });
  const section = page.locator('.control-section');
  await section.getByRole('button', { name: '审阅与决定' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认批准并暂停' }).click();
  await expect(section.getByText('请求结果未确认，请先核查；不会自动重试。')).toBeVisible();
  expect((await state(request, id)).status).toBe('paused');
  await expect(section.getByRole('button', { name: '恢复任务', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: '核查并解锁' }).click();
  await page.getByRole('dialog').getByRole('button', { name: '已核查，解除操作锁' }).click();
  await expect(section.getByRole('button', { name: '恢复任务', exact: true })).toBeEnabled();
  expect(decisions).toBe(1);
});
