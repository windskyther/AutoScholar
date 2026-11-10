import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { UsageCounts, UsageSummary } from '../src/usage-types';

const zero: UsageCounts = { calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, unknown_usage_calls: 0, failed_calls: 0 };
const measured: UsageCounts = { calls: 8, input_tokens: 1200, output_tokens: 800, total_tokens: 2000, unknown_usage_calls: 1, failed_calls: 1 };
async function fixture(page: Page, empty = false, failure = false, cache = false, priced = false) {
  const queries: URL[] = [];
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    expect(route.request().method()).toBe('GET');
    expect(route.request().headers().authorization).toBeUndefined();
    if (url.pathname === '/api/local-access') return route.fulfill({ json: { mode: 'local_proxy' } });
    if (url.pathname.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture',
      capabilities: { llm_configured: false, web_search_configured: false, task_streaming: false, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', model_usage: true } } });
    if (url.pathname === '/api/workbench/projects') return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    if (url.pathname === '/api/workbench/usage') {
      queries.push(url);
      if (failure) return route.fulfill({ status: 503, headers: { 'X-Request-ID': 'ff0cf6b9-e4cd-4d78-8aa4-5b864c2019c6' }, json: { error: { code: 'workbench_store_unavailable' } } });
      const days = Number(url.searchParams.get('days')) as UsageSummary['days'];
      const group = url.searchParams.get('group_by') as UsageSummary['group_by'];
      const summary: UsageSummary = { group_by: group, days, utc_offset_minutes: 480, recorded_since: '2026-11-07T00:00:00Z',
        trend_from: '2026-11-01', totals: empty ? zero : { ...measured, ...(cache ? {
          cached_input_tokens: 600, uncached_input_tokens: 400, cache_known_calls: 1,
          unknown_cache_calls: 7, cache_known_input_tokens: 1000, cache_hit_rate: 0.6,
        } : {}), ...(priced ? { estimated_cny: 0.00285, estimated_usd: 0.00125, cost_known_calls: 2, unknown_cost_calls: 6 } : {}) }, ...(cache ? { stages: [{ id: 'refining', label: 'refining', ...measured }] } : {}), groups: empty ? [] : [
          { id: group === 'api' ? 'reader' : 'model/中文 *', label: group === 'api' ? '阅读 API' : 'model/中文 *', ...measured },
        ], daily: Array.from({ length: days || 90 }, (_, index) => {
          const count = days || 90; const active = index - (count - 7);
          const tokens = active < 0 ? 0 : [100, 150, 250, 300, 400, 500, 300][active];
          return { date: new Date(Date.UTC(2026, 10, 8 - count + index)).toISOString().slice(0, 10),
            ...(empty || active < 0 ? zero : { calls: active === 6 ? 2 : 1, input_tokens: tokens * .6, output_tokens: tokens * .4,
              total_tokens: tokens, unknown_usage_calls: active === 6 ? 1 : 0, failed_calls: active === 5 ? 1 : 0 }) };
        }),
        options: empty ? { apis: [], models: [] } : { apis: [{ id: 'reader', name: '阅读 API' }, { id: 'writer', name: '写作 API' }], models: ['model/中文 *', 'second-model'] }, has_more_groups: false };
      return route.fulfill({ json: summary });
    }
    throw new Error('Unexpected usage fixture request ' + url.pathname);
  });
  return queries;
}

test('cost estimates keep currencies separate and report incomplete price coverage', async ({ page }) => {
  await fixture(page, false, false, true, true);
  await page.goto('/usage');
  const costs = page.getByRole('region', { name: '模型费用估算' });
  await expect(costs).toContainText('CNY 0.00285 · USD 0.00125');
  await expect(costs).toContainText('已估算 2 次；6 次缺少价格或用量信息');
});

test('usage totals, dimension and API/model/date filters stay read-only and survive refresh', async ({ page }) => {
  const queries = await fixture(page);
  await page.goto('/usage');
  await expect(page.getByRole('heading', { name: '用量统计', exact: true })).toBeVisible();
  await expect(page.getByTestId('usage-totals')).toContainText('2,000');
  await expect(page.getByText('1 次调用的用量尚未确认', { exact: true })).toBeVisible();
  await page.getByText('按模型', { exact: true }).click();
  await expect(page.getByRole('heading', { name: '模型用量分布' })).toBeVisible();
  await page.getByLabel('API 配置', { exact: true }).click();
  await page.locator('.ant-select-dropdown:visible').getByTitle('阅读 API', { exact: true }).click();
  await expect.poll(() => queries.at(-1)?.searchParams.get('api_id')).toBe('reader');
  await page.getByLabel('模型筛选', { exact: true }).click();
  await page.locator('.ant-select-dropdown:visible').getByTitle('model/中文 *', { exact: true }).click();
  await expect.poll(() => queries.at(-1)?.searchParams.get('model')).toBe('model/中文 *');
  await page.getByLabel('时间范围', { exact: true }).click();
  await page.getByText('最近 7 天', { exact: true }).click();
  await expect.poll(() => queries.at(-1)?.searchParams.get('days')).toBe('7');
  await expect(page.getByRole('img', { name: '每日已确认 Token趋势' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '用量统计', exact: true })).toBeVisible();
});

test('cache totals preserve unknown reports and show the known-input denominator', async ({ page }) => {
  await fixture(page, false, false, true);
  await page.goto('/usage');
  const cache = page.getByRole('region', { name: '缓存复用' });
  await expect(cache).toContainText('600');
  await expect(cache).toContainText('400');
  await expect(cache).toContainText('60.0%');
  await expect(page.getByRole('region', { name: '研究阶段用量' })).toContainText('独立复筛');
});

test('a supplier without cache details displays unknown instead of zero hits', async ({ page }) => {
  await fixture(page);
  await page.goto('/usage');
  const cache = page.getByRole('region', { name: '缓存复用' });
  await expect(cache.getByText('未提供', { exact: true })).toHaveCount(3);
});

test('usage charts and table fit a mobile viewport', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/usage');
  await expect(page.getByTestId('usage-totals')).toBeVisible();
  await expect(page.getByRole('img', { name: '每日已确认 Token趋势' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('usage-mobile.png'), fullPage: true });
});

test('empty usage explains historical coverage and shows no fabricated metrics', async ({ page }) => {
  await fixture(page, true);
  await page.goto('/usage');
  await expect(page.getByText('暂无调用记录。执行研究任务后，这里会自动更新。')).toBeVisible();
  await expect(page.getByText(/旧任务仍可在任务详情查看/)).toBeVisible();
  await expect(page.getByRole('img', { name: /每日/ })).toHaveCount(0);
});

test('usage failures offer a manual reload without any model request', async ({ page }) => {
  await fixture(page, false, true);
  await page.goto('/usage');
  await expect(page.getByRole('button', { name: '手动重试查询' })).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('ff0cf6b9-e4cd-4d78-8aa4-5b864c2019c6');
});

test('a first visit waits for both lazy code and data before animating the actual page', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.addInitScript(() => {
    const entries: { path: string; heading: string | null }[] = [];
    (window as unknown as { entries: typeof entries }).entries = entries;
    document.addEventListener('animationstart', (event) => {
      if (event.animationName === 'page-enter') entries.push({ path: location.pathname,
        heading: (event.target as Element).querySelector('h1')?.textContent ?? null });
    });
  });
  const entries = () => page.evaluate(() => (window as unknown as { entries: { path: string; heading: string | null }[] }).entries);
  let releaseCode!: () => void; let releaseData!: () => void;
  let codeRequested = false; let dataRequested = false;
  const code = new Promise<void>((resolve) => { releaseCode = resolve; });
  const data = new Promise<void>((resolve) => { releaseData = resolve; });
  await fixture(page);
  await page.route('**/src/usage-page.tsx*', async (route) => { codeRequested = true; await code; await route.continue(); });
  await page.route('**/api/workbench/usage?*', async (route) => { dataRequested = true; await data; await route.fallback(); });
  await page.goto('/projects');
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '用量统计', exact: true }).click();
  await expect.poll(() => codeRequested).toBe(true);
  await expect(page.locator('.loading, .ant-spin-spinning, .anticon-loading, .anticon-spin')).toHaveCount(0);
  await expect.poll(() => page.getByRole('main').innerText()).toBe('');
  await page.waitForTimeout(250);
  expect((await entries()).filter((entry) => entry.path === '/usage')).toHaveLength(0);
  releaseCode();
  await expect.poll(() => dataRequested).toBe(true);
  await expect(page.locator('.loading, .ant-spin-spinning, .anticon-loading, .anticon-spin')).toHaveCount(0);
  await expect(page.getByText('正在读取数据', { exact: true })).toHaveCount(0);
  await page.waitForTimeout(250);
  await expect(page.locator('.page-transition')).toHaveCount(0);
  expect((await entries()).filter((entry) => entry.path === '/usage')).toHaveLength(0);
  releaseData();
  await expect(page.getByTestId('usage-totals')).toBeVisible();
  await expect.poll(async () => (await entries()).filter((entry) => entry.path === '/usage')).toEqual([{ path: '/usage', heading: '用量统计' }]);
  await page.getByText('按模型', { exact: true }).click();
  await expect(page.getByRole('heading', { name: '模型用量分布' })).toBeVisible();
  expect((await entries()).filter((entry) => entry.path === '/usage')).toHaveLength(1);
  await page.reload();
  await expect(page.getByTestId('usage-totals')).toBeVisible();
  await expect.poll(async () => (await entries()).filter((entry) => entry.path === '/usage')).toEqual([{ path: '/usage', heading: '用量统计' }]);
});

test('route entry is brief, filters do not remount it, and reduced motion disables it', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await fixture(page);
  await page.goto('/projects');
  await page.getByRole('link', { name: '用量统计', exact: true }).click();
  await expect(page.getByTestId('usage-totals')).toBeVisible();
  const frame = page.locator('.page-transition');
  await expect(frame).toHaveCSS('animation-name', 'page-enter');
  await expect(frame).toHaveCSS('animation-duration', '0.48s');
  const sameFrame = await frame.elementHandle();
  await page.getByText('按模型', { exact: true }).click();
  await expect(page.getByRole('heading', { name: '模型用量分布' })).toBeVisible();
  expect(await frame.evaluate((element, original) => element === original, sameFrame)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('usage-desktop.png'), fullPage: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(frame).toHaveCSS('animation-name', 'none');
});
