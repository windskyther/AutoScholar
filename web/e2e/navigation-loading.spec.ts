import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

function gate() {
  let release!: () => void;
  const wait = new Promise<void>(resolve => { release = resolve; });
  return { wait, release };
}

async function setup(page: Page, delayed: string, failure = false) {
  const response = gate(); let requested = false;
  await page.addInitScript(() => {
    const record = window as unknown as { navigationLoaders: number };
    record.navigationLoaders = 0;
    new MutationObserver(() => {
      if (document.querySelector('.loading, .ant-spin-spinning, .anticon-loading, .anticon-spin')) record.navigationLoaders++;
    }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  });
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    expect(request.method()).toBe('GET');
    if (path === delayed) { requested = true; await response.wait; }
    if (path === delayed && failure) return route.fulfill({ status: 503, json: { error: { code: 'workbench_store_unavailable' } } });
    if (path.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, task_streaming: false,
      research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native',
    } } });
    if (path === '/api/workbench/projects') return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    if (path === '/api/workbench/models') return route.fulfill({ json: { items: [] } });
    throw Error('Unexpected navigation request: ' + path);
  });
  return { response, requested: () => requested };
}

async function expectNoLoading(page: Page) {
  await expect(page.locator('.loading, .ant-spin-spinning, .anticon-loading, .anticon-spin')).toHaveCount(0);
  await expect(page.getByText('正在读取数据', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { navigationLoaders: number }).navigationLoaders)).toBe(0);
}

for (const [path, label, title] of [['/projects', '项目与任务', '项目'], ['/models', '模型管理', '模型管理']]) {
  test(`switching to ${path} remains silent throughout a delayed response`, async ({ page }) => {
    const state = await setup(page, '/api/workbench' + path);
    await page.goto('/'); await expect(page.locator('#home-title')).toBeVisible();
    await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: label, exact: true }).click();
    await expect.poll(state.requested).toBe(true);
    await page.waitForTimeout(200);
    await expectNoLoading(page);
    expect(await page.getByRole('main').innerText()).toBe('');
    state.response.release();
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    await expectNoLoading(page);
    await expect(page.locator('.page-transition')).toHaveCSS('animation-duration', '0.48s');
  });
}

test('an initial connection check has no visible loading indicator', async ({ page }) => {
  const state = await setup(page, '/api/workbench/session');
  await page.goto('/'); await expect.poll(state.requested).toBe(true);
  await expectNoLoading(page);
  state.response.release();
  await expect(page.locator('#home-title')).toBeVisible();
  await expectNoLoading(page);
});

test('a failed page query shows its retry action without showing a loader first', async ({ page }) => {
  const state = await setup(page, '/api/workbench/models', true);
  await page.goto('/'); await expect(page.locator('#home-title')).toBeVisible();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理', exact: true }).click();
  await expect.poll(state.requested).toBe(true); await expectNoLoading(page);
  state.response.release();
  await expect(page.getByRole('button', { name: '手动重试查询', exact: true })).toBeVisible();
  await expectNoLoading(page);
});

test('leaving a pending page does not leak a loading overlay or replace the new page', async ({ page }) => {
  const state = await setup(page, '/api/workbench/models');
  await page.goto('/'); await expect(page.locator('#home-title')).toBeVisible();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理', exact: true }).click();
  await expect.poll(state.requested).toBe(true);
  await page.getByRole('link', { name: 'AutoScholar', exact: true }).click();
  await expect(page.locator('#home-title')).toBeVisible();
  state.response.release();
  await page.waitForTimeout(200); await expectNoLoading(page);
  await expect(page.locator('#home-title')).toBeVisible();
});
