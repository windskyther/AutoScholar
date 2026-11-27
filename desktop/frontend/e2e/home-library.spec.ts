import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    expect(request.method()).toBe('GET');
    if (path.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, task_streaming: false,
      research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native',
    } } });
    if (path === '/api/workbench/projects') return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    throw Error('Unexpected home request: ' + path);
  });
});

test('desktop settings clears the model cache only after confirmation', async ({ page }) => {
  await page.addInitScript(() => { window.__autoscholarSession = 'desktop-fixture'; });
  let bytes = 1048576;
  let deletions = 0;
  await page.route('**/api/desktop/**', route => {
    const request = route.request();
    expect(request.headers()['x-autoscholar-session']).toBe('desktop-fixture');
    if (request.url().endsWith('/settings')) {
      return route.fulfill({ json: { directory: 'fixture', profiles: [] } });
    }
    if (request.method() === 'DELETE') { bytes = 0; deletions++; }
    return route.fulfill({ json: { bytes } });
  });
  await page.goto('/');
  await page.getByRole('link', { name: '桌面设置' }).click();
  const clear = page.getByRole('button', { name: '清除模型缓存' });
  await expect(page.getByText('已占用 1 MiB')).toBeVisible();
  await clear.click();
  await page.getByRole('button', { name: /取\s*消/ }).click();
  expect(deletions).toBe(0);
  await clear.click();
  await page.getByRole('button', { name: /^清\s*除$/ }).click();
  await expect(page.getByText('已占用 0 MiB')).toBeVisible();
  await expect(clear).toBeDisabled();
  expect(deletions).toBe(1);
});

test('mode cards retain distinct choices when opened with the keyboard or restored after reload', async ({ page }) => {
  await page.goto('/');
  const trigger = page.getByRole('link', { name: '论文综述', exact: true });
  await trigger.focus(); await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\?research=literature_review$/);
  await page.reload();
  await expect(page.getByRole('heading', { name: '论文综述 · 选择项目', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('link', { name: 'AutoScholar', exact: true }).click();
  await page.getByRole('link', { name: '项目证据库', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?research=evidence$/);
  await expect(page.getByRole('heading', { name: '项目证据库 · 选择项目', exact: true })).toBeVisible();
});

test('the white home and direct research links fit a narrow screen with reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(page.locator('.page-transition')).toHaveCSS('animation-name', 'none');
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.body.clientWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath('home-mobile.png'), fullPage: true });
  await page.getByRole('link', { name: '研究方案', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?research=research_proposal$/);
  await expect(page.getByRole('heading', { name: '研究方案 · 选择项目', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.body.clientWidth)).toBe(true);
});

test('larger reading text reflows the home and keeps direct links usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  await page.addStyleTag({ content: ':root { font-size: 200%; }' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.body.clientWidth)).toBe(true);
  for (const card of await page.locator('.research-card').all()) {
    expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  await page.getByRole('link', { name: '项目文档分析', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?research=document_analysis$/);
  await expect(page.getByRole('heading', { name: '项目文档分析 · 选择项目', exact: true })).toBeVisible();
});

test('maximized workspace uses the available width as the viewport grows', async ({ page }) => {
  await page.goto('/');
  const library = page.locator('.research-library');
  for (const width of [1440, 1920, 2560]) {
    await page.setViewportSize({ width, height: 1080 });
    const bounds = await library.boundingBox();
    expect(bounds!.width).toBeGreaterThan(width * 0.9);
    const cards = page.locator('.research-card');
    const rows = await cards.evaluateAll(items => new Set(items.map(item => item.getBoundingClientRect().top)).size);
    expect(rows).toBe(width >= 1920 ? 1 : 2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }
});
