import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
const AUTH = { Authorization: 'Bearer public-test-fixture-token' };
test.describe.configure({ mode: 'serial' });

test.afterAll(async ({ request }) => {
  await request.post('http://127.0.0.1:18009/__fixture/stop', { headers: AUTH });
});
async function connect(page: import('@playwright/test').Page) {
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill('public-test-fixture-token');
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await page.getByRole('link', { name: '中文研究项目' }).click();
  await page.getByRole('link', { name: '比较 MNIST 模型' }).click();
}
test('evidence and report preview escape HTML; bearer export verifies real bytes', async ({ page, request }) => {
  await connect(page);
  const section = page.getByRole('region', { name: '任务资源' });
  await expect(section.getByText('公开 MNIST 证据')).toBeVisible();
  await section.getByRole('button', { name: '展开证据' }).click();
  await expect(section.getByText('公开测试主张')).toBeVisible();
  const link = section.getByRole('link'); await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  await section.getByRole('tab', { name: '文件与报告' }).click();
  const row = section.getByRole('row').filter({ hasText: 'reports/report.md' });
  await row.getByRole('button', { name: '预览' }).click();
  await expect(page.getByRole('dialog').getByText('# 中文公开报告', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).resourceXSS)).toBeUndefined();
  await page.getByRole('button', { name: '关闭预览' }).click();
  const downloaded = page.waitForEvent('download'); await row.getByRole('button', { name: '导出报告' }).click();
  const download = await downloaded; expect(download.suggestedFilename()).toContain('report.md');
  const bytes = await readFile((await download.path())!); expect(bytes.toString('utf8')).toContain('# 中文公开报告');
  const status = await request.get('http://127.0.0.1:18009/__fixture/status');
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0 });
});
test('experiment details show absent measurements honestly; mobile resources remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await connect(page);
  const section = page.getByRole('region', { name: '任务资源' });
  await section.getByRole('tab', { name: '实验对比' }).click();
  await expect(section.getByText('public-browser-mnist')).toBeVisible();
  await section.getByRole('button', { name: '展开实验' }).click();
  await expect(section.getByText('暂无可比较的实测指标，不使用模拟数据填补。')).toBeVisible();
  await section.getByRole('tab', { name: '文件与报告' }).click();
  await expect(section.getByRole('button', { name: '导出报告' })).toBeVisible();
});
