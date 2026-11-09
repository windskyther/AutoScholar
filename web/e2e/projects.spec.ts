import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const time = '2026-11-05T00:00:00Z';
const target = { id: 'project-delete', name: '可删除项目', description: null, created_at: time, updated_at: time };
const kept = { ...target, id: 'project-keep', name: '保留项目' };

async function fixture(page: Page, mode: 'success' | 'unknown' | 'active' = 'success') {
  let removed = false;
  let deletes = 0;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/local-access') return route.fulfill({ status: 404, json: { error: { code: 'not_found' } } });
    expect(request.headers().authorization).toBeUndefined();
    if (request.method() === 'DELETE' && path === `/api/workbench/projects/${target.id}`) {
      deletes++;
      if (mode === 'active') return route.fulfill({ status: 409, json: { error: { code: 'project_has_active_tasks' } } });
      removed = true;
      if (mode === 'unknown') return route.fulfill({ status: 500, json: { error: { code: 'internal_error' } } });
      return route.fulfill({ status: 204 });
    }
    expect(request.method()).toBe('GET');
    if (path === '/api/workbench/session') return route.fulfill({ json: {
      status: 'connected', authentication: 'local', api_version: 'fixture',
      capabilities: { llm_configured: false, web_search_configured: false, research_backend: 'native',
        filesystem_backend: 'native', experiment_backend: 'native', task_streaming: false, project_deletion: true },
    } });
    if (path === '/api/workbench/projects') return route.fulfill({ json: {
      items: removed ? [kept] : [target, kept], total: removed ? 1 : 2, limit: 20, offset: 0,
    } });
    if (path === `/api/workbench/projects/${target.id}`) return route.fulfill(removed
      ? { status: 404, json: { error: { code: 'project_not_found' } } } : { json: target });
    if (path.endsWith('/tasks') || path.endsWith('/documents')) return route.fulfill({ json: {
      project_id: target.id, items: [], total: 0, limit: 20, offset: 0,
    } });
    throw new Error('Unmocked project request');
  });
  await page.goto('/projects');
  await expect(page.getByRole('link', { name: target.name, exact: true })).toBeVisible();
  return { deletes: () => deletes };
}

test('requires the project name, supports cancellation and removes only the selected project', async ({ page }) => {
  const state = await fixture(page);
  await expect(page.getByText(/Phase 9|单操作者|手动确认提交|未进行联测|检索后端/)).toHaveCount(0);
  const trigger = page.getByRole('button', { name: `删除项目 ${target.name}`, exact: true });
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: '删除项目', exact: true });
  await expect(dialog.getByRole('button', { name: '确认删除项目' })).toBeDisabled();
  await dialog.getByLabel('输入项目名称以确认删除').fill('错误名称');
  await expect(dialog.getByRole('button', { name: '确认删除项目' })).toBeDisabled();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(state.deletes()).toBe(0);
  await trigger.click();
  await dialog.getByLabel('输入项目名称以确认删除').fill(target.name);
  await dialog.getByRole('button', { name: '确认删除项目' }).dblclick();
  await expect(page.getByRole('link', { name: target.name, exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: kept.name, exact: true })).toBeVisible();
  expect(state.deletes()).toBe(1);
});

test('unknown deletion is checked by reading instead of sending another delete', async ({ page }) => {
  const state = await fixture(page, 'unknown');
  await page.getByRole('button', { name: `删除项目 ${target.name}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除项目', exact: true });
  await dialog.getByLabel('输入项目名称以确认删除').fill(target.name);
  await dialog.getByRole('button', { name: '确认删除项目' }).click();
  await expect(dialog.getByText('删除结果尚未确认，请先核对结果。')).toBeVisible();
  expect(state.deletes()).toBe(1);
  await dialog.getByRole('button', { name: '核对删除结果' }).click();
  await expect(page.getByRole('link', { name: target.name, exact: true })).toHaveCount(0);
  expect(state.deletes()).toBe(1);
});

test('deletion from project details reports unfinished tasks without removing the project', async ({ page }) => {
  const state = await fixture(page, 'active');
  await page.getByRole('link', { name: target.name, exact: true }).click();
  await page.getByRole('button', { name: `删除项目 ${target.name}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除项目', exact: true });
  await dialog.getByLabel('输入项目名称以确认删除').fill(target.name);
  await dialog.getByRole('button', { name: '确认删除项目' }).click();
  await expect(dialog.getByText('项目中还有未结束或待核对的任务，请先处理后再删除。')).toBeVisible();
  expect(state.deletes()).toBe(1);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('heading', { name: target.name, exact: true })).toBeVisible();
});
