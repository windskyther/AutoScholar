import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const time = '2026-10-06T00:00:00Z';
const project = { id: 'project-a', name: '公开研究项目', description: '浏览器夹具', created_at: time, updated_at: time };
const budget = { steps: 5, replans: 1, model_calls: 3, tool_calls: 5, search_queries: 2, code_repairs: 1,
  training_runs: 1, sandbox_runs: 3, total_tokens: 1000, wall_seconds: 60 };
const document = { id: 'doc-ready', project_id: project.id, original_filename: 'public.pdf', title: '可用 PDF',
  content_type: 'application/pdf', sha256: 'a'.repeat(64), size_bytes: 100, status: 'ready', page_count: 1, chunk_count: 2,
  embedding_model: 'fixture', index_version: 1, error_code: null, error_message: null, created_at: time, updated_at: time };
type Entry = { method: string; path: string; key?: string; body?: string };

async function fixture(page: Page, options: { loseTaskAck?: boolean; loseProjectAck?: boolean; unauthorizedTask?: boolean } = {}) {
  const calls: Entry[] = [];
  const projects = [project];
  const docs = [{ ...document }, { ...document, id: 'doc-failed', title: '失败 PDF', status: 'failed', error_code: 'pdf_text_not_extractable' }];
  let task: Record<string, unknown> | null = null;
  let lost = false;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    expect(request.headers().authorization).toBe('Bearer ' + TOKEN);
    calls.push({ method, path, key: request.headers()['idempotency-key'], body: request.postData() ?? undefined });
    let body: unknown;
    let status = 200;
    if (path === '/api/workbench/session') body = { status: 'connected', authentication: 'single_operator_bearer', api_version: 'fixture', capabilities: {
      llm_configured: true, web_search_configured: true, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native',
      task_streaming: false, document_max_bytes: 1024, document_max_pages: 10, budget_limits: budget,
    } };
    else if (path === '/api/workbench/projects' && method === 'POST') {
      if (options.loseProjectAck) { await route.abort(); return; }
      const input = request.postDataJSON() as { name: string; description: string | null };
      body = { ...project, ...input, id: 'project-created' }; projects.push(body as typeof project); status = 201;
    } else if (path === '/api/workbench/projects') body = { items: projects, total: projects.length, limit: 20, offset: 0 };
    else if (/^\/api\/workbench\/projects\/[^/]+$/.test(path)) body = projects.find((item) => path.endsWith('/' + item.id));
    else if (path.endsWith('/tasks') && method === 'GET') body = { items: task ? [task] : [], total: task ? 1 : 0, limit: 20, offset: 0 };
    else if (path.endsWith('/documents') && method === 'GET') {
      const items = path.includes('/project-a/') ? docs : [];
      body = { items, total: items.length, limit: 20, offset: 0 };
    }
    else if (path.endsWith('/documents') && method === 'POST') {
      body = { ...document, id: 'doc-uploaded', title: '上传 PDF', status: 'queued' }; docs.push(body as typeof document); status = 202;
    } else if (/\/documents\/.+$/.test(path)) {
      const id = path.split('/documents/')[1].split('/')[0];
      const item = docs.find((entry) => entry.id === id)!;
      item.status = method === 'DELETE' ? 'deleting' : 'queued';
      if (method === 'DELETE') { await route.fulfill({ status: 202, body: '' }); return; }
      body = item;
    } else if (path === '/api/workbench/agent/tasks') {
      if (options.unauthorizedTask) { status = 401; body = { error: { code: 'experiment_auth_required' } }; }
      else {
        const input = request.postDataJSON();
        task = { task_id: 'task-submitted', project_id: project.id, parent_task_id: null, objective: input.objective,
          mode: 'autonomous', status: 'queued', metrics: {}, error_code: null, created_at: time, updated_at: time };
        if (options.loseTaskAck && !lost) { lost = true; await route.abort(); return; }
        await new Promise((resolve) => setTimeout(resolve, 150));
        body = { task_id: task.task_id, status: 'queued', created: !lost, status_url: '/workbench/agent/tasks/task-submitted' }; status = 202;
      }
    } else if (path.endsWith('/overview')) body = { task, children: { items: [], total: 0, limit: 20, offset: 0 }, current_plan: null,
      execution: { status: 'queued', stage: 'planner', plan_version: 1, checkpoint_sequence: 1, budget_used: {}, budget_limits: budget,
        active_seconds: 0, pending_call_count: 0, error_code: null }, resources: { evidence: 0, experiments: 0, artifacts: 0 },
      answer: null, answer_truncated: false, usage_scope: 'root_task_only', monetary_cost: null };
    else throw new Error('Unexpected fixture route: ' + method + ' ' + path);
    await route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) });
  });
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await expect(page.getByRole('link', { name: project.name })).toBeVisible();
  return calls;
}
async function enterProject(page: Page) { await page.getByRole('link', { name: project.name }).click(); }
async function draftTask(page: Page) {
  await enterProject(page);
  await page.getByRole('button', { name: '创建研究任务' }).click();
  await page.getByLabel('研究目标', { exact: true }).fill('公开的 MNIST 验收目标');
}
const consent = '我确认将此研究目标交给 Worker 自动执行，并接受可能产生的 API 费用。';

test('creates a project after input validation and navigates to it', async ({ page }) => {
  const calls = await fixture(page);
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByText('填写 1–200 字符的项目名称')).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(0);
  await page.getByLabel('项目名称').fill('新增中文项目');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新增中文项目' })).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(1);
});

test('unknown project creation cannot be blindly retried', async ({ page }) => {
  const calls = await fixture(page, { loseProjectAck: true });
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByLabel('项目名称').fill('不明创建结果');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByText('请求结果未确认，请先核查；不会自动重试。')).toBeVisible();
  await expect(page.getByRole('button', { name: '创建项目', exact: true })).toBeDisabled();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(1);
});

test('validates PDF uploads locally, then shows asynchronous queued state', async ({ page }) => {
  const calls = await fixture(page); await enterProject(page);
  await page.getByRole('button', { name: '上传 PDF', exact: true }).click();
  await page.getByLabel('PDF 文件', { exact: true }).setInputFiles({ name: '.env', mimeType: 'text/plain', buffer: Buffer.from('public fixture only') });
  await page.getByRole('button', { name: '确认上传并索引' }).click();
  await expect(page.getByText('只支持 PDF 文件。')).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(0);
  await page.getByLabel('PDF 文件', { exact: true }).setInputFiles({ name: 'public.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4\npublic fixture') });
  await page.getByRole('button', { name: '确认上传并索引' }).click();
  await expect(page.getByText('PDF 已保存并排队，尚未完成解析与索引。')).toBeVisible();
  await expect(page.getByText('等待处理', { exact: true })).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(1);
});

test('PDF deletion requires explicit confirmation and queues exactly one request', async ({ page }) => {
  const calls = await fixture(page); await enterProject(page);
  const row = page.getByRole('row').filter({ hasText: '可用 PDF' });
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  expect(calls.filter((item) => item.method === 'DELETE')).toHaveLength(0);
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByText('等待删除', { exact: true })).toBeVisible();
  expect(calls.filter((item) => item.method === 'DELETE')).toHaveLength(1);
});

test('PDF retries and reindexing are confirmed and do not launch a task', async ({ page }) => {
  const calls = await fixture(page); await enterProject(page);
  await page.getByRole('row').filter({ hasText: '失败 PDF' }).getByRole('button', { name: '重试', exact: true }).click();
  await page.getByRole('button', { name: '确认重试', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: '失败 PDF' }).getByText('等待处理')).toBeVisible();
  await page.getByRole('row').filter({ hasText: '可用 PDF' }).getByRole('button', { name: '重建索引' }).click();
  await page.getByRole('button', { name: '确认重建', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: '可用 PDF' }).getByText('等待处理')).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(2);
  expect(calls.some((item) => item.path.includes('/agent/'))).toBe(false);
});

test('task requires cost consent, ready documents and respects server budget, even on double click', async ({ page }) => {
  const calls = await fixture(page); await draftTask(page);
  await page.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByText('请确认自动执行及可能产生费用')).toBeVisible();
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(0);
  await page.getByLabel('项目 PDF 范围').click();
  await page.getByText('选择已就绪 PDF', { exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('row').filter({ hasText: '失败 PDF' }).getByRole('checkbox')).toBeDisabled();
  await dialog.getByRole('row').filter({ hasText: '可用 PDF' }).getByRole('checkbox').check();
  await expect(page.getByLabel('总 Token 预算')).toHaveValue('1000');
  await page.getByRole('checkbox', { name: consent }).check();
  await page.getByRole('button', { name: '确认提交并自动执行' }).dblclick();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submissions = calls.filter((item) => item.path === '/api/workbench/agent/tasks');
  expect(submissions).toHaveLength(1);
  expect(submissions[0].key).toBeTruthy();
  expect(JSON.parse(submissions[0].body!)).toMatchObject({ document_ids: ['doc-ready'], budget, mode: 'autonomous', project_id: project.id });
});

test('lost task acknowledgement freezes the original draft and survives route changes before explicit retry', async ({ page }) => {
  const calls = await fixture(page, { loseTaskAck: true }); await draftTask(page);
  await page.getByRole('checkbox', { name: consent }).check();
  await page.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByRole('button', { name: '用原编号确认 / 重试' })).toBeVisible();
  await expect(page.getByLabel('研究目标', { exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '保留请求并关闭' }).click();
  await page.getByRole('link', { name: '项目与任务' }).click();
  await page.getByRole('button', { name: '核查原提交' }).click();
  await page.getByRole('button', { name: '用原编号确认 / 重试' }).click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submissions = calls.filter((item) => item.path === '/api/workbench/agent/tasks');
  expect(submissions).toHaveLength(2);
  expect(submissions[0].key).toBe(submissions[1].key);
  expect(submissions[0].body).toBe(submissions[1].body);
  await expect(page.getByRole('button', { name: '核查原提交' })).toHaveCount(0);
});

test('task authentication failure clears the connection instead of retrying the write', async ({ page }) => {
  const calls = await fixture(page, { unauthorizedTask: true }); await draftTask(page);
  await page.getByRole('checkbox', { name: consent }).check();
  await page.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByRole('heading', { name: '连接研究工作台' })).toBeVisible();
  expect(calls.filter((item) => item.path === '/api/workbench/agent/tasks')).toHaveLength(1);
});

test('task budget and explicit consent remain usable on a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const calls = await fixture(page); await draftTask(page);
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds!.width).toBeLessThanOrEqual(390);
  await page.getByLabel('总 Token 预算').fill('500');
  await page.getByRole('checkbox', { name: consent }).check();
  const submit = page.getByRole('button', { name: '确认提交并自动执行' });
  await submit.scrollIntoViewIfNeeded();
  await expect(submit).toBeInViewport();
  expect(await page.evaluate(() => window.document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/phase9c-mobile-task.png', fullPage: true });
  await submit.click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submissions = calls.filter((item) => item.path === '/api/workbench/agent/tasks');
  expect(submissions).toHaveLength(1);
  expect(JSON.parse(submissions[0].body!).budget.total_tokens).toBe(500);
});
