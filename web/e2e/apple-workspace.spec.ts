import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const time = '2026-11-09T00:00:00Z';
const projects = [
  { id: 'project-vision', name: '视觉表征与多模态研究', description: '追踪视觉模型的新方法，梳理从图像理解到多模态推理的研究脉络。' },
  { id: 'project-efficient', name: '高效语言模型综述', description: '探索模型压缩、低秩适配与低成本推理，让研究走向实际应用。' },
  { id: 'project-retrieval', name: '检索增强生成', description: '对比检索策略与证据组织方式，构建可追溯的知识与结论。' },
].map(project => ({ ...project, created_at: time, updated_at: time }));

async function fixture(page: Page) {
  const requests: string[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request(); const url = new URL(request.url());
    requests.push(request.method() + ' ' + url.pathname);
    expect(request.method()).toBe('GET');
    if (url.pathname.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (url.pathname.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, task_streaming: false,
      research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', project_deletion: true,
    } } });
    if (url.pathname === '/api/workbench/projects') return route.fulfill({ json: { items: projects, total: 3, limit: 20, offset: 0 } });
    if (url.pathname === '/api/workbench/models') return route.fulfill({ json: { items: [] } });
    if (url.pathname === '/api/workbench/usage') {
      const zero = { calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, unknown_usage_calls: 0, failed_calls: 0 };
      return route.fulfill({ json: { group_by: url.searchParams.get('group_by'), days: 30, utc_offset_minutes: 480,
        recorded_since: null, trend_from: '2026-10-11', totals: zero, groups: [], options: { apis: [], models: [] }, has_more_groups: false,
        daily: Array.from({ length: 30 }, (_, index) => ({ date: new Date(Date.UTC(2026, 9, 11 + index)).toISOString().slice(0, 10), ...zero })),
      } });
    }
    if (url.pathname === '/api/workbench/projects/project-vision') return route.fulfill({ json: projects[0] });
    if (url.pathname.endsWith('/tasks') || url.pathname.endsWith('/documents')) return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    throw new Error('Unexpected workspace request: ' + url.pathname);
  });
  return requests;
}

test('top glass dock replaces the sidebar, preserves navigation and renders project cards', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const requests = await fixture(page);
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  const dock = page.getByRole('navigation', { name: '主导航' });
  await expect(dock.getByRole('link')).toHaveCount(3);
  await expect(page.getByRole('complementary')).toHaveCount(0);
  await expect(dock.getByRole('link', { name: '项目与任务' })).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('.project-card')).toHaveCount(3);
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'svg');
  const position = await dock.boundingBox();
  expect(position && Math.abs(position.x + position.width / 2 - 720)).toBeLessThan(2);
  expect(position?.y).toBeLessThan(40);
  await page.waitForTimeout(200);
  const refracted = await dock.screenshot();
  const originalFilter = await page.locator('.glass-backdrop').evaluate(element => {
    const style = (element as HTMLElement).style;
    const value = style.backdropFilter;
    style.backdropFilter = 'blur(3px) saturate(1.1)';
    return value;
  });
  await page.waitForTimeout(100);
  expect(refracted.equals(await dock.screenshot())).toBe(false);
  await page.locator('.glass-backdrop').evaluate((element, value) => { (element as HTMLElement).style.backdropFilter = value; }, originalFilter);
  await page.screenshot({ path: test.info().outputPath('desktop.png'), fullPage: true });
  await page.getByRole('link', { name: projects[0].name }).click();
  await expect(page.getByRole('heading', { name: projects[0].name, exact: true })).toBeVisible();
  await expect(dock.getByRole('link', { name: '项目与任务' })).toHaveAttribute('aria-current', 'page');
  await dock.getByRole('link', { name: '模型管理' }).click();
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
  await expect(dock.getByRole('link', { name: '模型管理' })).toHaveAttribute('aria-current', 'page');
  await dock.getByRole('link', { name: '用量统计' }).click();
  await expect(page.getByRole('heading', { name: '用量统计', exact: true })).toBeVisible();
  await expect(dock.getByRole('link', { name: '用量统计' })).toHaveAttribute('aria-current', 'page');
  expect(requests.every(request => request.startsWith('GET '))).toBe(true);
  expect(errors).toEqual([]);
});

for (const width of [320, 390]) {
  test(`dock and project actions fit ${width}px without horizontal page scrolling`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await fixture(page); await page.goto('/');
    await expect(page.getByRole('link', { name: projects[0].name })).toBeVisible();
    const dock = page.getByRole('navigation', { name: '主导航' });
    for (const link of await dock.getByRole('link').all()) {
      const rect = await link.boundingBox();
      expect(rect!.width).toBeGreaterThanOrEqual(44); expect(rect!.height).toBeGreaterThanOrEqual(44);
      expect(rect!.x).toBeGreaterThanOrEqual(0); expect(rect!.x + rect!.width).toBeLessThanOrEqual(width);
    }
    const main = await page.getByRole('main').boundingBox(); const nav = await dock.boundingBox();
    expect(main!.y).toBeGreaterThan(nav!.y + nav!.height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.getByRole('button', { name: '新建项目', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '新建项目' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: '新建项目' })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`mobile-${width}.png`), fullPage: true });
  });
}

test('reduced motion and increased contrast update the glass material and keep navigation usable', async ({ page }) => {
  await fixture(page); await page.goto('/');
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'svg');
  await page.emulateMedia({ reducedMotion: 'reduce', contrast: 'more' });
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'css');
  expect(await page.locator('.glass-backdrop').evaluate(element => getComputedStyle(element).backdropFilter)).toBe('none');
  expect(await page.locator('.page-transition').evaluate(element => getComputedStyle(element).animationName)).toBe('none');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理' }).click();
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
  await page.emulateMedia({ reducedMotion: 'no-preference', contrast: 'no-preference' });
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'svg');
  await expect(page.locator('.glass-definitions filter')).toHaveCount(1);
});

test('dark appearance follows the system while the geometric wallpaper stays static', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page); await page.goto('/');
  await expect(page.locator('.project-card')).toHaveCount(3);
  expect(await page.locator('.app-content').evaluate(element => getComputedStyle(element).backgroundColor)).toBe('rgba(31, 31, 34, 0.97)');
  expect(await page.locator('.geometric-backdrop').evaluate(element => getComputedStyle(element).animationName)).toBe('none');
  await page.waitForTimeout(200);
  await page.screenshot({ path: test.info().outputPath('dark.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'light' });
  await expect.poll(() => page.locator('.app-content').evaluate(element => getComputedStyle(element).backgroundColor)).toBe('rgba(255, 255, 255, 0.96)');
});

test('keyboard users can skip to content and select every dock destination', async ({ page }) => {
  await fixture(page); await page.goto('/');
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: '跳至内容' })).toBeFocused();
  await page.keyboard.press('Enter'); await expect(page.getByRole('main')).toBeFocused();
  const models = page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理' });
  await models.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
  await expect(models).toHaveAttribute('aria-current', 'page');
});

test('a missing canvas context falls back without blocking navigation or creating extra SVGs', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', { value: () => null }));
  await fixture(page); await page.goto('/');
  await expect(page.locator('.project-card')).toHaveCount(3);
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'css');
  await expect(page.locator('.glass-definitions')).toHaveCount(0);
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理' }).click();
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
});

test('resize regenerates one bounded displacement map while route switches retain the dock', async ({ page }) => {
  await fixture(page); await page.goto('/');
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'svg');
  const filter = page.locator('.glass-definitions filter'); const id = await filter.getAttribute('id');
  for (const width of [320, 1440, 390, 1200]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(async () => Number(await filter.getAttribute('width'))).toBe(await page.locator('.glass-surface').evaluate(element => element.clientWidth));
    await expect(filter).toHaveCount(1); await expect(filter).toHaveAttribute('id', id!);
  }
  const dock = page.getByRole('navigation', { name: '主导航' });
  for (let index = 0; index < 3; index++) {
    await dock.getByRole('link', { name: '模型管理' }).click();
    await dock.getByRole('link', { name: '项目与任务' }).click();
  }
  await expect(filter).toHaveCount(1); await expect(filter).toHaveAttribute('id', id!);
});

test('project pagination loads each real page and navigating from a scrolled collection starts at the top', async ({ page }) => {
  await fixture(page);
  const entries = Array.from({ length: 25 }, (_, index) => ({ ...projects[0], id: `collection-${index}`, name: `研究项目 ${index + 1}` }));
  await page.route('**/api/workbench/projects?*', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'));
    return route.fulfill({ json: { items: entries.slice(offset, offset + 20), total: 25, limit: 20, offset } });
  });
  await page.goto('/'); await expect(page.locator('.project-card')).toHaveCount(20);
  await page.getByTitle('2', { exact: true }).click();
  await expect(page.locator('.project-card')).toHaveCount(5);
  await expect(page.getByRole('link', { name: '研究项目 25', exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '模型管理' }).click();
  await expect(page.getByRole('heading', { name: '模型管理', exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test('forced colors replace the wallpaper and glass with readable system surfaces', async ({ page }) => {
  await page.emulateMedia({ forcedColors: 'active' });
  await fixture(page); await page.goto('/');
  await expect(page.locator('.glass-surface')).toHaveAttribute('data-refraction', 'css');
  expect(await page.locator('.geometric-backdrop').evaluate(element => getComputedStyle(element).backgroundImage)).toBe('none');
  await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '项目与任务' })).toBeVisible();
  await expect(page.getByRole('button', { name: '新建项目', exact: true })).toBeVisible();
});
