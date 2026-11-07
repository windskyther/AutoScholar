import { expect, test } from '@playwright/test';

test('optional prices are saved with a model without contacting any provider', async ({ page }) => {
  let submitted: Record<string, unknown> | undefined;
  const items: Record<string, unknown>[] = [];
  await page.route('**/api/**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, task_streaming: false,
      research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native',
      model_management: true, model_pricing: true,
    } } });
    if (path === '/api/workbench/models' && request.method() === 'GET') return route.fulfill({ json: { items } });
    if (path === '/api/workbench/models' && request.method() === 'POST') {
      submitted = request.postDataJSON();
      const { api_key: _secret, ...profile } = submitted!;
      items.push({ ...profile, enabled: true, created_at: '2026-11-07T00:00:00Z' });
      return route.fulfill({ status: 201, json: items[0] });
    }
    throw new Error('Unexpected request ' + path);
  });
  await page.goto('/models');
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('配置名称', { exact: true }).fill('计价阅读模型');
  await dialog.getByLabel('API 接口地址', { exact: true }).fill('https://models.example/v1');
  await dialog.getByLabel('模型名称', { exact: true }).fill('fixture-model');
  await dialog.getByLabel('API Key', { exact: true }).fill('public-browser-fixture-key');
  await dialog.getByRole('checkbox', { name: '设置可选单价，用于估算模型费用' }).check();
  await dialog.getByRole('spinbutton', { name: /普通输入单价 \/ 百万 Token$/ }).fill('2');
  await dialog.getByRole('spinbutton', { name: '缓存命中输入单价 / 百万 Token（可选）', exact: true }).fill('0.5');
  await dialog.getByRole('spinbutton', { name: /输出单价 \/ 百万 Token$/ }).fill('8');
  await dialog.getByRole('button', { name: '保存模型', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(submitted?.pricing).toEqual({ currency: 'CNY', input_per_million: 2, cached_input_per_million: 0.5, output_per_million: 8 });
  expect(submitted).not.toHaveProperty('pricing_enabled');
  await expect(page.getByText('CNY · 已设置单价')).toBeVisible();
});
