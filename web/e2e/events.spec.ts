import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const time = '2026-10-06T00:00:00Z';
const project = { id: 'project-a', name: '公开事件项目', description: null, created_at: time, updated_at: time };
const task = { task_id: 'task-a', project_id: project.id, parent_task_id: null, objective: '公开事件目标', mode: 'autonomous',
  status: 'running', metrics: {}, error_code: null, created_at: time, updated_at: time };
const ready = 'event: ready\ndata: {"task_id":"task-a","status":"running","durable":true}\n\n';
const end = 'event: end\ndata: {"task_id":"task-a","status":"succeeded","reason":"terminal"}\n\n';
const event = (sequence: number) => `id: ${sequence}\nevent: workflow\ndata: ` + JSON.stringify({
  task_id: task.task_id, sequence, kind: 'checkpoint_saved', created_at: time, payload: { status: 'succeeded', stage: 'done' },
}) + '\n\n';
type Entry = { path: string; cursor: string | undefined; method: string };
async function fixture(page: Page, mode: 'duplicate' | 'gap' | 'retry' | 'outage' | 'cancel') {
  const calls: Entry[] = []; let count = 0;
  await page.route('**/api/**', async (route) => {
    const req = route.request(); const path = new URL(req.url()).pathname;
    expect(req.headers().authorization).toBe('Bearer ' + TOKEN);
    calls.push({ path, cursor: req.headers()['last-event-id'], method: req.method() });
    let payload: unknown;
    if (path.endsWith('/stream')) {
      count += 1;
      if (mode === 'outage') { await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"workbench_stream_unavailable"}}' }); return; }
      const body = mode === 'duplicate' ? ready + event(1) + event(1) + end
        : mode === 'gap' ? ready + event(3) + end
        : mode === 'cancel' ? ready
        : count === 1 ? ready + event(1) : ready + event(1) + event(2) + end;
      await route.fulfill({ status: 200, contentType: 'text/event-stream; charset=utf-8', body }); return;
    }
    if (path.endsWith('/session')) payload = { status: 'connected', authentication: 'single_operator_bearer', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', task_streaming: true,
    } };
    else if (path === '/api/workbench/projects') payload = { items: [project], total: 1, limit: 20, offset: 0 };
    else if (path.endsWith('/projects/project-a')) payload = project;
    else if (path.endsWith('/documents')) payload = { items: [], total: 0, limit: 20, offset: 0 };
    else if (path.endsWith('/tasks')) payload = { items: [task], total: 1, limit: 20, offset: 0 };
    else if (path.endsWith('/events')) payload = { task_id: task.task_id, status: 'running', durable: true, items: [], next_cursor: 0, has_more: false, has_older: false };
    else if (path.endsWith('/overview')) payload = { task, children: { items: [], total: 0, limit: 20, offset: 0 }, current_plan: null, execution: null,
      resources: { evidence: 0, experiments: 0, artifacts: 0 }, answer: null, answer_truncated: false, usage_scope: 'root_task_only', monetary_cost: null };
    else throw new Error('Unexpected event fixture route: ' + path);
    await route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(payload) });
  });
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: project.name }).click();
  await page.getByRole('link', { name: task.objective }).click();
  return calls;
}

test('duplicate delivery is rendered once, including on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const calls = await fixture(page, 'duplicate');
  await expect(page.getByText('任务已结束，事件已同步', { exact: true })).toBeVisible();
  await expect(page.getByText('#1', { exact: true })).toHaveCount(1);
  expect(await page.evaluate(() => window.document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(calls.every((entry) => entry.method === 'GET')).toBe(true);
  await page.locator('.event-section').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/phase9d-mobile-events.png', fullPage: true });
});
test('an event gap stops and requires a fresh history read', async ({ page }) => {
  const calls = await fixture(page, 'gap');
  await expect(page.getByText('事件序号不连续，请重新加载记录核查。')).toBeVisible();
  await expect(page.getByText('#3', { exact: true })).toHaveCount(0);
  expect(calls.filter((entry) => entry.path.endsWith('/stream'))).toHaveLength(1);
});
test('read-only reconnect keeps the cursor and ignores a duplicate before the next event', async ({ page }) => {
  const calls = await fixture(page, 'retry');
  await expect(page.getByText('任务已结束，事件已同步', { exact: true })).toBeVisible();
  await expect(page.getByText('#1', { exact: true })).toHaveCount(1);
  await expect(page.getByText('#2', { exact: true })).toHaveCount(1);
  expect(calls.filter((entry) => entry.path.endsWith('/stream')).map((entry) => entry.cursor)).toEqual(['0', '1']);
});
test('persistent read failure caps retries and leaves a manual reload action', async ({ page }) => {
  test.setTimeout(45000);
  const calls = await fixture(page, 'outage');
  await expect(page.getByText('事件连接已停止', { exact: true })).toBeVisible({ timeout: 35000 });
  expect(calls.filter((entry) => entry.path.endsWith('/stream'))).toHaveLength(6);
  await expect(page.getByRole('button', { name: '重新加载记录' })).toBeEnabled();
  expect(calls.every((entry) => entry.method === 'GET')).toBe(true);
});
test('route navigation aborts backoff and does not leave a background stream', async ({ page }) => {
  const calls = await fixture(page, 'cancel');
  await expect(page.getByText('断线后补齐事件', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '项目与任务' }).click();
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  const count = calls.filter((entry) => entry.path.endsWith('/stream')).length;
  await page.waitForTimeout(1300);
  expect(calls.filter((entry) => entry.path.endsWith('/stream'))).toHaveLength(count);
});
