import { expect, test } from '@playwright/test';

test('project evidence supports original links, withdrawal, history pagination and recheck policy', async ({ page }) => {
  const time = '2026-11-09T00:00:00Z';
  const project = { id: 'project-a', name: '研究项目', description: null, created_at: time, updated_at: time };
  const source = { title: '原始论文', url: 'https://example.org/paper', kind: 'paper', quote: '可以逐字核对的原文证据',
    published_at: '2026-10-01', fetched_at: time, availability: 'summary_only', page: null, truncated: false, recheck_due: true };
  const claim = { id: 'ff0cf6b9-e4cd-4d78-8aa4-5b864c2019c6', task_id: 'origin-a', text: '已审查的低秩适配结论', objective: '原始研究目标',
    enabled: true, status: 'ready', sources: [source], reuse_count: 2, created_at: time };
  let writes = 0; let imports = 0; let policy = { web_days: 7, paper_days: 30 };
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (path.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: false, web_search_configured: false, task_streaming: false, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', project_evidence: true,
    } } });
    if (path.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path === '/api/workbench/projects/project-a') return route.fulfill({ json: project });
    if (path.endsWith('/tasks') || path.endsWith('/documents')) return route.fulfill({ json: { items: [], total: 0, limit: 20, offset: 0 } });
    if (path.endsWith('/evidence/policy')) {
      if (request.method() === 'PUT') { policy = request.postDataJSON(); writes++; }
      return route.fulfill({ json: policy });
    }
    if (path.endsWith('/evidence/backfill')) {
      const input = request.postDataJSON(); imports++;
      expect(input.after).toBe(imports === 1 ? null : 'cursor-next');
      return route.fulfill({ json: { queued_tasks: 1, pending_claims: 0, skipped_tasks: 0, next_cursor: imports === 1 ? 'cursor-next' : null } });
    }
    if (path.endsWith('/disable') || path.endsWith('/enable')) {
      claim.enabled = path.endsWith('/enable'); writes++;
      return route.fulfill({ json: { id: claim.id, enabled: claim.enabled } });
    }
    if (path.endsWith('/evidence')) return route.fulfill({ json: { items: [claim], total: 1, limit: 20, offset: 0 } });
    throw new Error('Unexpected evidence route: ' + path);
  });
  await page.goto('/projects/project-a');
  await page.getByRole('button', { name: '项目证据库', exact: true }).click();
  await expect(page.getByRole('heading', { name: '项目证据库', exact: true })).toBeVisible();
  await expect(page.getByText(claim.text, { exact: true })).toBeVisible();
  await expect(page.locator('body')).not.toContainText(claim.id);
  await page.getByRole('button', { name: '展开行' }).click();
  await expect(page.getByRole('link', { name: '原始论文' })).toHaveAttribute('href', source.url);
  await expect(page.getByText('需要重新核验', { exact: true })).toBeVisible();
  await expect(page.getByText(source.quote)).toBeVisible();
  await page.getByRole('button', { name: '移除', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '移除', exact: true }).click();
  await expect(page.getByRole('button', { name: '恢复', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '恢复', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '恢复', exact: true }).click();
  await expect(page.getByRole('button', { name: '移除', exact: true })).toBeVisible();
  for (const label of ['整理历史成果', '继续整理下一批']) {
    await page.getByRole('button', { name: new RegExp(label) }).click();
    await page.getByRole('dialog').getByRole('button', { name: '开始整理' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  }
  expect(imports).toBe(2);
  await page.getByText('资料复核期限', { exact: true }).click();
  await page.getByRole('spinbutton', { name: '网页复核间隔（天）' }).fill('10');
  await page.getByRole('button', { name: '保存复核期限' }).click();
  await expect.poll(() => policy.web_days).toBe(10);
  expect(writes).toBe(3);
});
