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

test('research mode search filters, clears and explains an empty result without querying providers', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  const search = page.getByRole('searchbox', { name: '搜索研究方式' });
  await search.fill('PDF');
  await expect(page.locator('.research-card')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '了解项目文档分析', exact: true })).toBeVisible();
  await search.fill('nothing-matches-this');
  await expect(page.locator('.research-card')).toHaveCount(0);
  await expect(page.getByText('没有找到相关的研究方式，试试“论文”“资料”或“PDF”。')).toBeVisible();
  await page.getByRole('button', { name: '清空搜索', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(page.locator('.research-card')).toHaveCount(6);
});

test('mode guides support keyboard dismissal and lead to real projects without creating a task', async ({ page }) => {
  await page.goto('/');
  const trigger = page.getByRole('button', { name: '了解论文综述', exact: true });
  await trigger.focus(); await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: '论文综述', exact: true });
  await expect(dialog.getByText('可以这样开始', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.getByRole('button', { name: '了解项目证据库', exact: true }).click();
  await page.getByRole('dialog', { name: '项目证据库', exact: true }).getByRole('link', { name: '查看项目', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible();
});

test('the white home and research guide fit a narrow screen with reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(page.locator('.page-transition')).toHaveCSS('animation-name', 'none');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.getByRole('button', { name: '了解研究方案', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '研究方案', exact: true });
  await expect(dialog.getByRole('link', { name: '查看项目', exact: true })).toBeVisible();
  expect(await dialog.evaluate(element => parseFloat(getComputedStyle(element).animationDuration))).toBeLessThanOrEqual(.001);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.screenshot({ path: test.info().outputPath('guide-mobile.png') });
});

test('larger reading text reflows the home and keeps search and guides usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.locator('.research-card')).toHaveCount(6);
  await page.addStyleTag({ content: ':root { font-size: 200%; }' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  for (const card of await page.locator('.research-card').all()) {
    expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  const search = page.getByRole('searchbox', { name: '搜索研究方式' });
  await search.fill('PDF');
  await expect(page.locator('.research-card')).toHaveCount(1);
  const trigger = page.getByRole('button', { name: '了解项目文档分析', exact: true });
  await trigger.click();
  await expect(page.getByRole('dialog', { name: '项目文档分析', exact: true }).getByRole('link', { name: '查看项目', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await page.getByRole('button', { name: '清空搜索', exact: true }).click();
  await expect(page.locator('.research-card')).toHaveCount(6);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});
