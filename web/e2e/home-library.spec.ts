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
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.screenshot({ path: test.info().outputPath('home-mobile.png'), fullPage: true });
  await page.getByRole('link', { name: '研究方案', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?research=research_proposal$/);
  await expect(page.getByRole('heading', { name: '研究方案 · 选择项目', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
});

test('larger reading text reflows the home and keeps direct links usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  await page.addStyleTag({ content: ':root { font-size: 200%; }' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  for (const card of await page.locator('.research-card').all()) {
    expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  await page.getByRole('link', { name: '项目文档分析', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?research=document_analysis$/);
  await expect(page.getByRole('heading', { name: '项目文档分析 · 选择项目', exact: true })).toBeVisible();
});
