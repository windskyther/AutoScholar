import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const token = 'public-task-actions-token';
const time = '2026-11-06T00:00:00Z';
const project = { id: 'project-actions', name: '目标管理项目', description: null, created_at: time, updated_at: time };
const goal = { task_id: 'goal-a', project_id: project.id, parent_task_id: null, objective: '中文研究 / 目标',
  mode: 'autonomous', status: 'succeeded', metrics: {}, error_code: null, created_at: time, updated_at: time };
const keep = { ...goal, task_id: 'goal-keep', objective: '保留此研究目标' };
const markdown = '# 中文研究 / 目标\n\n## 完整研究结论\n\n\\(y=x^2\\) [E1]\n\n|来源|结论|\n|---|---|\n|论文|保留完整中文与数学公式|\n';

async function fixture(page: Page, mode: 'success' | 'unknown' | 'active' | 'empty' | 'cancelled' = 'success') {
  let removed = false;
  const shownGoal = mode === 'cancelled' ? { ...goal, status: 'cancelled' } : goal;
  const calls: { method: string; path: string }[] = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (path === '/api/local-access') return route.fulfill({ status: 404, json: { error: { code: 'not_found' } } });
    expect(request.headers().authorization).toBe('Bearer ' + token);
    calls.push({ method: request.method(), path });
    if (request.method() === 'DELETE') {
      expect(path).toBe(`/api/workbench/projects/${project.id}/tasks/${goal.task_id}`);
      if (mode === 'active') return route.fulfill({ status: 409, json: { error: { code: 'task_has_active_operations' } } });
      removed = true;
      return mode === 'unknown' ? route.fulfill({ status: 500, json: { error: { code: 'internal_error' } } }) : route.fulfill({ status: 204 });
    }
    expect(request.method()).toBe('GET');
    if (path.endsWith('/session')) return route.fulfill({ json: {
      status: 'connected', authentication: 'single_operator_bearer', api_version: 'fixture',
      capabilities: { llm_configured: false, web_search_configured: false, research_backend: 'native', filesystem_backend: 'native',
        experiment_backend: 'native', task_streaming: true, resource_browser: true, task_deletion: true, markdown_export: true },
    } });
    if (path === '/api/workbench/projects') return route.fulfill({ json: { items: [project], total: 1, limit: 20, offset: 0 } });
    if (path === `/api/workbench/projects/${project.id}`) return route.fulfill({ json: project });
    if (path.endsWith('/documents')) return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    if (path.endsWith('/tasks')) return route.fulfill({ json: { items: removed ? [keep] : [shownGoal, keep], total: removed ? 1 : 2, limit: 20, offset: 0 } });
    if (path.endsWith('/overview')) return removed ? route.fulfill({ status: 404, json: { error: { code: 'workbench_task_not_found' } } }) : route.fulfill({ json: {
      task: shownGoal, children: { items: [], total: 0, limit: 20, offset: 0 }, current_plan: null, execution: null,
      resources: { evidence: 2, experiments: 0, artifacts: 0 }, answer: mode === 'empty' ? null : '页面中的截断预览', answer_truncated: mode !== 'empty',
      usage_scope: 'root_task_only', monetary_cost: null,
    } });
    if (path.endsWith('/events')) return route.fulfill({ json: { task_id: goal.task_id, status: 'succeeded', durable: false,
      items: [], next_cursor: 0, has_more: false, has_older: false } });
    if (path.endsWith('/resources/evidence')) return route.fulfill({ json: { task_id: goal.task_id, total: 2, limit: 20, offset: 0, items: [
      { id: 'evidence-a', task_id: 'child', citation_key: 'E1', source_type: 'web', title: '来源论文', url: 'https://example.org/paper',
        authors: [], year: 2026, claim: '公开研究结论', excerpt: '原文证据', text_truncated: false, document_id: null, page: null, section: null },
      { id: 'evidence-b', task_id: 'child', citation_key: 'E2', source_type: 'document', title: '本地摘录', url: null,
        authors: [], year: null, claim: '本地研究结论', excerpt: '本地证据', text_truncated: false, document_id: 'doc', page: 1, section: null },
    ] } });
    if (path.endsWith('/output/markdown')) return route.fulfill({ contentType: 'text/markdown; charset=utf-8', body: markdown });
    throw new Error('Unexpected task action route: ' + path);
  });
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(token);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: project.name, exact: true }).click();
  return calls;
}

test('events start collapsed and citation links are directly available on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const calls = await fixture(page);
  await page.getByRole('link', { name: goal.objective, exact: true }).click();
  const events = page.locator('details.event-section');
  await expect(events).not.toHaveAttribute('open', '');
  await expect(page.getByText('此历史任务没有实时记录', { exact: true })).not.toBeVisible();
  await expect.poll(() => calls.filter((call) => call.path.endsWith('/events')).length).toBeGreaterThan(0);
  await events.locator('summary').click();
  await expect(page.getByText('此历史任务没有实时记录', { exact: true })).toBeVisible();
  await events.locator('summary').click();
  await expect(events).not.toHaveAttribute('open', '');
  const link = page.getByRole('link', { name: '查看引用 E1 的来源', exact: true });
  await expect(link).toHaveAttribute('href', 'https://example.org/paper');
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  await expect(page.getByRole('link', { name: '来源论文', exact: true })).toHaveAttribute('href', 'https://example.org/paper');
  await expect(page.getByRole('link', { name: '查看引用 E2 的来源', exact: true })).toHaveCount(0);
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('exports full UTF-8 Markdown with formulas instead of the truncated preview', async ({ page }) => {
  const calls = await fixture(page);
  await page.getByRole('link', { name: goal.objective, exact: true }).click();
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 Markdown', exact: true }).click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe('中文研究 _ 目标-goal-a.md');
  expect((await readFile((await download.path())!)).toString('utf8')).toBe(markdown);
  expect(calls.filter((call) => call.path.endsWith('/output/markdown'))).toHaveLength(1);
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

test('output export stays disabled before a result exists', async ({ page }) => {
  const calls = await fixture(page, 'empty');
  await page.getByRole('link', { name: goal.objective, exact: true }).click();
  await expect(page.getByRole('button', { name: '导出 Markdown', exact: true })).toBeDisabled();
  expect(calls.filter((call) => call.path.endsWith('/output/markdown'))).toHaveLength(0);
});

test('goal deletion supports cancellation, one submission and keeps the sibling', async ({ page }) => {
  const calls = await fixture(page);
  const trigger = page.getByRole('button', { name: `删除研究目标 ${goal.objective}`, exact: true });
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: '删除研究目标', exact: true });
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(0);
  await trigger.click();
  await dialog.getByRole('button', { name: '删除此目标', exact: true }).dblclick();
  await expect(page.getByRole('link', { name: goal.objective, exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: keep.objective, exact: true })).toBeVisible();
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
});

test('unknown deletion is verified with a read and never automatically resent', async ({ page }) => {
  const calls = await fixture(page, 'unknown');
  await page.getByRole('button', { name: `删除研究目标 ${goal.objective}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除研究目标', exact: true });
  await dialog.getByRole('button', { name: '删除此目标', exact: true }).click();
  await expect(dialog.getByText('删除结果尚未确认，请先核对结果。')).toBeVisible();
  await dialog.getByRole('button', { name: '核对删除结果', exact: true }).click();
  await expect(page.getByRole('link', { name: goal.objective, exact: true })).toHaveCount(0);
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
});

test('a changed execution state refuses deletion and preserves the goal', async ({ page }) => {
  await fixture(page, 'active');
  await page.getByRole('button', { name: `删除研究目标 ${goal.objective}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除研究目标', exact: true });
  await dialog.getByRole('button', { name: '删除此目标', exact: true }).click();
  await expect(dialog.getByText('任务或关联子任务尚未完全停止，请等待取消完成后再删除。')).toBeVisible();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('link', { name: goal.objective, exact: true })).toBeVisible();
});

test('a fully cancelled goal can be removed while its sibling stays visible', async ({ page }) => {
  const calls = await fixture(page, 'cancelled');
  await expect(page.getByText('已取消', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: `删除研究目标 ${goal.objective}`, exact: true }).click();
  await page.getByRole('dialog', { name: '删除研究目标', exact: true }).getByRole('button', { name: '删除此目标', exact: true }).click();
  await expect(page.getByRole('link', { name: goal.objective, exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: keep.objective, exact: true })).toBeVisible();
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
});
