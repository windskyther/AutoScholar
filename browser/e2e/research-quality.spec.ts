import { expect, test } from '@playwright/test';

test('quality audit opens on demand with source links, review reasons and no internal IDs', async ({ page }) => {
  let auditReads = 0;
  const task = { task_id: 'task-a', project_id: 'project-a', parent_task_id: null,
    objective: '公开资料精筛验收', mode: 'autonomous', status: 'succeeded', metrics: {},
    error_code: null, created_at: '2026-11-07T00:00:00Z', updated_at: '2026-11-07T00:00:00Z' };
  const assessment = { source_id: 'E1', verdict: 'keep', relevance: 0.9, time_fit: 'unknown',
    reason: '原始来源与主题相关', quote: '可核对的原始资料引文' };
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    expect(route.request().method()).toBe('GET');
    if (path.endsWith('/local-access')) return route.fulfill({ json: { mode: 'local_proxy' } });
    if (path.endsWith('/session')) return route.fulfill({ json: { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: true, web_search_configured: true, task_streaming: false,
      research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native', research_quality: true,
    } } });
    if (path.endsWith('/overview')) return route.fulfill({ json: { task,
      children: { items: [], total: 0, limit: 20, offset: 0 }, current_plan: null,
      execution: { status: 'succeeded', stage: 'done', plan_version: 1, checkpoint_sequence: 15,
        budget_used: {}, budget_limits: null, active_seconds: 10, pending_call_count: 0,
        error_code: null, research_depth: 'standard' },
      resources: { evidence: 1, experiments: 0, artifacts: 0 }, answer: '综合研究报告 [E1]',
      answer_truncated: false, usage_scope: 'root_task_only', monetary_cost: null,
    } });
    if (path.endsWith('/quality')) {
      auditReads++;
      return route.fulfill({ json: { depth: 'standard', phase: 'done', accepted: true,
        reads: 0, rounds: 1, supplemental_queries: 0, sources: [{ reference: 'E1', title: '原始研究资料',
          url: 'https://example.org/study', published_at: null, availability: 'summary_only',
          truncated: false, read_attempted: true, initial: assessment, refined: assessment }],
        reviews: [{ round: 0, decision: 'revise', issues: [{ draft_quote: '需要修订的旧结论', reason: '原文只支持有限范围' }] },
          { round: 1, decision: 'accept', issues: [] }],
      } });
    }
    throw new Error('Unexpected fixture route: ' + path);
  });
  await page.goto('/projects/project-a/tasks/task-a');
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  expect(auditReads).toBe(0);
  await page.getByText('资料筛选与审稿', { exact: true }).click();
  await expect(page.getByText('结论已通过资料审查')).toBeVisible();
  await expect(page.getByRole('link', { name: '原始研究资料' })).toHaveAttribute('href', 'https://example.org/study');
  await expect(page.getByText('发表日期未确认 · 仅有摘要')).toBeVisible();
  await expect(page.getByText('原文只支持有限范围')).toBeVisible();
  await expect(page.getByText('原始来源与主题相关')).toHaveCount(2);
  await expect(page.locator('body')).not.toContainText('ff0cf6b9-e4cd-4d78-8aa4-5b864c2019c6');
});
