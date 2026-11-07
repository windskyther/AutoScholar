import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const modelId = '11111111-1111-4111-8111-111111111111';
const time = '2026-10-06T00:00:00Z';
const project = { id: 'project-a', name: '公开研究项目', description: '浏览器夹具', created_at: time, updated_at: time };
const budget = { steps: 5, replans: 1, model_calls: 3, tool_calls: 5, search_queries: 2, code_repairs: 1,
  training_runs: 1, sandbox_runs: 3, total_tokens: 1000, wall_seconds: 60 };
const document = { id: 'doc-ready', project_id: project.id, original_filename: 'public.pdf', title: '可用 PDF',
  content_type: 'application/pdf', sha256: 'a'.repeat(64), size_bytes: 100, status: 'ready', page_count: 1, chunk_count: 2,
  embedding_model: 'fixture', index_version: 1, error_code: null, error_message: null, created_at: time, updated_at: time };
type Entry = { method: string; path: string; key?: string; body?: string };

async function fixture(page: Page, options: { loseTaskAck?: boolean; loseProjectAck?: boolean; unauthorizedTask?: boolean; researchWorkflows?: boolean; tokenBudget?: number } = {}) {
  const calls: Entry[] = [];
  const limits = { ...budget, total_tokens: options.tokenBudget ?? budget.total_tokens };
  const projects = [project];
  const docs = [{ ...document }, { ...document, id: 'doc-failed', title: '失败 PDF', status: 'failed', error_code: 'pdf_text_not_extractable' }];
  let task: Record<string, unknown> | null = null;
  let lost = false;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/local-access') return route.fulfill({ status: 404, json: { error: { code: 'not_found' } } });
    const method = request.method();
    expect(request.headers().authorization).toBeUndefined();
    calls.push({ method, path, key: request.headers()['idempotency-key'], body: request.postData() ?? undefined });
    let body: unknown;
    let status = 200;
    if (path === '/api/workbench/session') body = { status: 'connected', authentication: 'local', api_version: 'fixture', capabilities: {
      llm_configured: true, web_search_configured: true, research_backend: 'native', filesystem_backend: 'native', experiment_backend: 'native',
      task_streaming: false, document_max_bytes: 1024, document_max_pages: 10, budget_limits: limits,
      research_workflows: options.researchWorkflows ?? false,
    } };
    else if (path === '/api/workbench/models') body = { items: [{ id: '11111111-1111-4111-8111-111111111111', name: 'Managed fixture', base_url: 'https://fixture.example/v1', model: 'fixture', compatibility_profile: 'auto', enabled: true, created_at: time }] };
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
      if (options.unauthorizedTask) { status = 401; body = { error: { code: 'local_access_denied' } }; }
      else {
        const input = request.postDataJSON();
        task = { task_id: 'task-submitted', project_id: project.id, parent_task_id: null, objective: input.objective,
          mode: 'autonomous', status: 'queued', metrics: {}, error_code: null, created_at: time, updated_at: time };
        if (options.loseTaskAck && !lost) { lost = true; await route.abort(); return; }
        await new Promise((resolve) => setTimeout(resolve, 150));
        body = { task_id: task.task_id, status: 'queued', created: !lost, status_url: '/workbench/agent/tasks/task-submitted' }; status = 202;
      }
    } else if (path.endsWith('/overview')) body = { task, children: { items: [], total: 0, limit: 20, offset: 0 }, current_plan: null,
      execution: { status: 'queued', stage: 'planner', plan_version: 1, checkpoint_sequence: 1, budget_used: {}, budget_limits: limits,
        active_seconds: 0, pending_call_count: 0, error_code: null }, resources: { evidence: 0, experiments: 0, artifacts: 0 },
      answer: null, answer_truncated: false, usage_scope: 'root_task_only', monetary_cost: null };
    else throw new Error('Unexpected fixture route: ' + method + ' ' + path);
    await route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body) });
  });
  await page.goto('/');
  await expect(page.getByRole('link', { name: project.name })).toBeVisible();
  return calls;
}
async function enterProject(page: Page) { await page.getByRole('link', { name: project.name }).click(); }
async function draftTask(page: Page) {
  await enterProject(page);
  await page.getByRole('button', { name: '创建研究任务' }).click();
  await page.getByLabel('研究目标', { exact: true }).fill('公开的 MNIST 验收目标');
}

test('an empty model manager blocks task submission without a fallback model', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/workbench/models', (route) => route.fulfill({ json: { items: [] } }));
  await draftTask(page);
  await expect(page.getByText('请先添加或选择一个可用模型')).toBeVisible();
  await expect(page.getByRole('button', { name: '确认提交并自动执行' })).toBeDisabled();
  await expect(page.getByText(/默认模型|默认 API/)).toHaveCount(0);
});

test('document analysis requires project PDFs and hides external search options', async ({ page }) => {
  await fixture(page);
  await draftTask(page);
  await page.getByLabel('任务类型', { exact: true }).click();
  await page.getByText('项目文档分析', { exact: true }).click();
  await expect(page.getByLabel('检索来源', { exact: true })).toHaveCount(0);
  await expect(page.getByText('全部已就绪 PDF', { exact: true })).toBeVisible();
  await expect(page.getByText('MNIST CPU 实验', { exact: true })).toHaveCount(0);
});
const consent = '我确认提交后自动执行研究任务，并接受可能产生的 API 费用。';

test('research defaults preserve the objective and explicitly disable unselected PDFs', async ({ page }) => {
  const calls = await fixture(page, { researchWorkflows: true });
  await draftTask(page);
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('资料调研', { exact: true })).toBeVisible();
  await expect(dialog.getByText('不使用 PDF', { exact: true })).toBeVisible();
  await expect(dialog.getByLabel('训练次数上限', { exact: true })).toHaveCount(0);
  const objective = '2026年10月CV领域的最新研究成果';
  await dialog.getByLabel('研究目标', { exact: true }).fill(objective);
  await dialog.getByRole('checkbox', { name: consent }).check();
  await dialog.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submitted = calls.filter((item) => item.method === 'POST' && item.path.endsWith('/agent/tasks'));
  expect(submitted).toHaveLength(1);
  expect(JSON.parse(submitted[0].body!)).toMatchObject({ objective, workflow_kind: 'research', document_ids: [] });
});

test('the advertised 500000-token allowance is available and submitted for a new task', async ({ page }) => {
  const calls = await fixture(page, { researchWorkflows: true, tokenBudget: 500000 });
  await enterProject(page);
  await page.getByRole('button', { name: '创建研究任务' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('研究目标', { exact: true }).fill('公开预算验收，不执行真实模型调用');
  const tokens = dialog.getByRole('spinbutton', { name: /总 Token 预算$/ });
  await expect(tokens).toHaveValue('500000');
  await expect(tokens).toHaveAttribute('aria-valuemax', '500000');
  await dialog.getByLabel('我确认提交后自动执行研究任务，并接受可能产生的 API 费用。').check();
  await dialog.getByRole('button', { name: '确认提交并自动执行', exact: true }).click();
  await expect.poll(() => calls.filter((call) => call.method === 'POST' && call.path.endsWith('/agent/tasks')).length).toBe(1);
  const request = calls.find((call) => call.method === 'POST' && call.path.endsWith('/agent/tasks'))!;
  expect(JSON.parse(request.body!).budget.total_tokens).toBe(500000);
});

test('literature selection uses a managed model and exposes no training controls', async ({ page }) => {
  const calls = await fixture(page, { researchWorkflows: true });
  await draftTask(page);
  const dialog = page.getByRole('dialog');
  const kind = dialog.getByLabel('任务类型', { exact: true });
  await kind.click();
  await kind.press('ArrowDown');
  await kind.press('Enter');
  await expect(dialog.getByLabel('训练次数上限', { exact: true })).toHaveCount(0);
  await dialog.getByRole('checkbox', { name: consent }).check();
  await dialog.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submitted = calls.find((item) => item.method === 'POST' && item.path.endsWith('/agent/tasks'))!;
  expect(JSON.parse(submitted.body!)).toMatchObject({ workflow_kind: 'literature_review', model_profile_id: '11111111-1111-4111-8111-111111111111' });
});

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
  await expect(page.getByLabel('总 Token 预算', { exact: true })).toHaveValue('1000');
  await page.getByRole('checkbox', { name: consent }).check();
  await page.getByRole('button', { name: '确认提交并自动执行' }).dblclick();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  const submissions = calls.filter((item) => item.path === '/api/workbench/agent/tasks');
  expect(submissions).toHaveLength(1);
  expect(submissions[0].key).toBeTruthy();
  const { code_repairs: _repairs, sandbox_runs: _sandbox, training_runs: _training, ...researchBudget } = budget;
  expect(JSON.parse(submissions[0].body!)).toMatchObject({ document_ids: ['doc-ready'], budget: researchBudget, mode: 'autonomous', workflow_kind: 'research', model_profile_id: modelId, project_id: project.id });
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
  await page.getByLabel('总 Token 预算', { exact: true }).fill('500');
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
