import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const time = '2026-11-06T00:00:00Z';
const project = { id: 'automatic-project', name: '自动连接项目', description: null, created_at: time, updated_at: time };
const session = { status: 'connected', authentication: 'single_operator_bearer', api_version: 'fixture',
  capabilities: { llm_configured: false, web_search_configured: false, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', task_streaming: false } };

async function fixture(page: Page, initial: 200 | 401 | 503 = 200) {
  let status = initial; const calls: string[] = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    expect(request.method()).toBe('GET');
    expect(request.headers().authorization).toBeUndefined();
    calls.push(path);
    if (path === '/api/local-access') return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path.endsWith('/session')) return route.fulfill(status === 200 ? { json: session } : {
      status, json: { error: { code: status === 401 ? 'experiment_auth_required' : 'local_proxy_not_configured' } },
    });
    if (path === '/api/workbench/projects') return route.fulfill({ json: { items: [project], total: 1, limit: 20, offset: 0 } });
    if (path.endsWith('/projects/' + project.id)) return route.fulfill({ json: project });
    if (path.endsWith('/tasks') || path.endsWith('/documents')) return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    throw new Error('Unexpected automatic connection request');
  });
  return { calls, recover: () => { status = 200; } };
}

test('opens and refreshes the local workbench without a login or browser credential', async ({ page }) => {
  await fixture(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /断开连接/ })).toHaveCount(0);
  await page.getByRole('link', { name: project.name, exact: true }).click();
  await page.reload();
  await expect(page.getByRole('heading', { name: project.name, exact: true })).toBeVisible();
  await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => Object.keys(localStorage).length + Object.keys(sessionStorage).length)).toBe(0);
});

for (const status of [401, 503] as const) {
  test(`backend ${status} offers a credentialless retry without automatic retry loops`, async ({ page }) => {
    const state = await fixture(page, status);
    await page.goto('/');
    await expect(page.getByRole('button', { name: '重试连接', exact: true })).toBeVisible();
    await expect(page.getByLabel('操作者 Token', { exact: true })).toHaveCount(0);
    const before = state.calls.filter((path) => path.endsWith('/session')).length;
    await page.waitForTimeout(500);
    expect(state.calls.filter((path) => path.endsWith('/session'))).toHaveLength(before);
    state.recover();
    await page.getByRole('button', { name: '重试连接', exact: true }).click();
    await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  });
}
