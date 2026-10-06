import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const time = '2026-10-06T00:00:00Z';
const project = { id: 'project-a', name: '中文研究项目', description: '本地浏览器测试夹具', created_at: time, updated_at: time };
const task = { task_id: 'task-a', project_id: project.id, parent_task_id: null,
  objective: '比较 MNIST 模型', mode: 'autonomous', status: 'paused',
  metrics: { total_tokens: 12 }, error_code: null, created_at: time, updated_at: time };
const session = { status: 'connected', authentication: 'single_operator_bearer', api_version: 'fixture',
  capabilities: { llm_configured: false, web_search_configured: false, research_backend: 'native',
    filesystem_backend: 'native', experiment_backend: 'native', task_streaming: false } };
const overview = { task, children: { items: [], total: 0, limit: 20, offset: 0 },
  current_plan: { version: 1, plan: { goal: task.objective, steps: [
    { id: 'research', type: 'research', description: '查找公开资料', dependencies: [], expected_output: '证据' },
  ] } }, execution: null, resources: { evidence: 2, experiments: 1, artifacts: 10 },
  answer: '<script>window.uiInjected=true</script>\n中文输出', answer_truncated: false,
  usage_scope: 'root_task_only', monetary_cost: null };

async function mockAPI(page: Page, sessionStatus = 200) {
  const requests: { method: string; path: string; authorized: boolean }[] = [];
  // Explicitly intercept API requests: these browser tests must not reach real services.
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push({ method: request.method(), path: url.pathname,
      authorized: request.headers().authorization === 'Bearer ' + TOKEN });
    if (request.method() !== 'GET') throw new Error('Unexpected non-read request');
    let body: unknown;
    let status = 200;
    if (url.pathname === '/api/workbench/session') {
      status = sessionStatus;
      body = status === 200 ? session : { error: { code: status === 401 ? 'experiment_auth_required' : 'experiment_api_not_configured' } };
    } else if (url.pathname === '/api/workbench/projects') body = { items: [project], total: 1, limit: 20, offset: 0 };
    else if (url.pathname === `/api/workbench/projects/${project.id}`) body = project;
    else if (url.pathname === `/api/workbench/projects/${project.id}/documents`) body = { items: [], total: 0, limit: 20, offset: 0 };
    else if (url.pathname === `/api/workbench/projects/${project.id}/tasks`) {
      const items = url.searchParams.get('status') === 'failed' ? [] : [task];
      body = { items, total: items.length, limit: 20, offset: 0 };
    } else if (url.pathname === `/api/workbench/tasks/${task.task_id}/overview`) body = overview;
    else throw new Error('Unmocked browser API route: ' + url.pathname);
    await route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) });
  });
  return requests;
}
async function connect(page: Page) {
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
}

test('connects, navigates true records, renders Chinese safely and keeps credentials out of storage', async ({ page }) => {
  const requests = await mockAPI(page);
  await connect(page);
  await page.getByRole('link', { name: project.name }).click();
  await page.getByRole('link', { name: task.objective }).click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  await expect(page.getByText('当前为只读快照，不是实时流')).toBeVisible();
  await expect(page.getByText('中文输出', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { uiInjected?: boolean }).uiInjected)).toBeUndefined();
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie }));
  expect(stored).not.toContain(TOKEN);
  expect(page.url()).not.toContain(TOKEN);
  expect(requests.every((r) => r.method === 'GET' && r.authorized)).toBeTruthy();
  await page.screenshot({ path: 'test-results/workbench-desktop.png', fullPage: true });
  await page.reload();
  await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveValue('');
});

test('filters tasks, disconnects and requires reauthentication', async ({ page }) => {
  await mockAPI(page);
  await connect(page);
  await page.getByRole('link', { name: project.name }).click();
  await page.getByRole('combobox', { name: '任务状态' }).click();
  await page.getByText('失败', { exact: true }).click();
  await expect(page.getByText('没有符合条件的研究任务。')).toBeVisible();
  await page.getByRole('button', { name: '断开连接' }).click();
  await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
});

for (const status of [401, 503]) {
  test(`connection ${status} shows actionable error with no automatic retries`, async ({ page }) => {
    const requests = await mockAPI(page, status);
    await connect(page);
    await expect(page.getByText(status === 401 ? '操作者 Token 无效，请重新连接。' : '服务端尚未配置 EXPERIMENT_API_TOKEN。')).toBeVisible();
    await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
    expect(requests).toHaveLength(1);
  });
}

test('connection page fits mobile and dev server refuses repository file access', async ({ page, request }) => {
  await mockAPI(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/connect');
  await expect(page.getByRole('button', { name: '连接本地 API' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  // Probe a harmless tracked file, not the actual .env or design document.
  const forbidden = await request.get('/@fs/D:/98281/deepscholar/README.md');
  expect(forbidden.status()).toBe(403);
  await page.screenshot({ path: 'test-results/workbench-mobile.png', fullPage: true });
});

test('malformed backend records show a query error instead of a blank page', async ({ page }) => {
  await mockAPI(page);
  await page.route('**/api/workbench/projects?*', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: null }) });
  });
  await connect(page);
  await expect(page.getByText('API 返回格式异常，请检查后端是否已升级。')).toBeVisible();
  await expect(page.getByRole('button', { name: '手动重试查询' })).toBeVisible();
  await expect(page.getByRole('button', { name: '断开连接' })).toBeVisible();
});

test('expired authenticated session clears cached pages and does not retry automatically', async ({ page }) => {
  await mockAPI(page);
  await connect(page);
  await expect(page.getByRole('link', { name: project.name })).toBeVisible();
  let failedRequests = 0;
  await page.route('**/api/workbench/projects?*', async (route) => {
    failedRequests += 1;
    await route.fulfill({ status: 401, contentType: 'application/json', body: '{}' });
  });
  await page.getByRole('button', { name: '刷新快照' }).click();
  await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveValue('');
  expect(failedRequests).toBe(1);
  await expect(page.getByRole('link', { name: project.name })).toHaveCount(0);
});
