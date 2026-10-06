import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

const TOKEN = 'public-test-fixture-token';
const fixtureURL = 'http://127.0.0.1:18009';
const authorization = { Authorization: 'Bearer ' + TOKEN };
const consent = '我确认将此研究目标交给 Worker 自动执行，并接受可能产生的 API 费用。';
// One shared, owned fixture; stop on failure and clean up once, never restart against a stopped DB.
test.describe.configure({ mode: 'serial' });

async function connect(page: Page) {
  await page.goto('/');
  await page.getByLabel('操作者 Token', { exact: true }).fill(TOKEN);
  await page.getByRole('button', { name: '连接本地 API' }).click();
  await expect(page.getByRole('link', { name: '中文研究项目' })).toBeVisible();
}
async function process(request: APIRequestContext) {
  const response = await request.post(fixtureURL + '/__fixture/process', { headers: authorization });
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ handled: true });
}
async function upload(page: Page, bytes: Buffer, filename: string, title: string) {
  await page.getByRole('button', { name: '上传 PDF', exact: true }).click();
  await page.getByLabel('PDF 文件', { exact: true }).setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: bytes });
  await page.getByLabel('文档标题（可选）').fill(title);
  const receipt = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/documents'));
  await page.getByRole('button', { name: '确认上传并索引' }).click();
  await receipt;
}

test.afterAll(async ({ request }) => {
  const status = await request.get(fixtureURL + '/__fixture/status');
  expect(await status.json()).toMatchObject({ model_calls: 0, sandbox_calls: 0 });
  const stop = await request.post(fixtureURL + '/__fixture/stop', { headers: authorization });
  expect(stop.status()).toBe(200);
  await expect.poll(async () => {
    try { await request.get(fixtureURL + '/__fixture/status', { timeout: 1000 }); return false; }
    catch { return true; }
  }, { timeout: 10000 }).toBe(true);
});

test('real project creation, PDF upload/parse/reindex/delete with fixture vectors and no provider calls', async ({ page, request }) => {
  await connect(page);
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByLabel('项目名称').fill('9C 隔离验收项目');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('heading', { name: '9C 隔离验收项目', exact: true })).toBeVisible();
  const projectId = new URL(page.url()).pathname.split('/')[2];
  const pdf = await request.get(fixtureURL + '/__fixture/pdf');
  const bytes = await pdf.body();
  await upload(page, bytes, 'public.pdf', '公开验收 PDF');
  await expect(page.getByText('等待处理', { exact: true })).toBeVisible();
  await process(request);
  await page.getByRole('button', { name: '刷新文档' }).click();
  const row = page.getByRole('row').filter({ hasText: '公开验收 PDF' });
  await expect(row.getByText('已就绪', { exact: true })).toBeVisible();
  const documents = await request.get(`/api/workbench/projects/${projectId}/documents`, { headers: authorization });
  const record = (await documents.json()).items[0];
  expect(record.page_count).toBe(1);
  expect(record.chunk_count).toBeGreaterThan(0);
  expect(record.embedding_model).toBe('test-embedding');

  await upload(page, bytes, 'copy.pdf', '重复上传');
  await expect(page.getByText('该 PDF 已存在于此项目，请刷新文档列表。')).toBeVisible();
  await page.keyboard.press('Escape');
  await row.getByRole('button', { name: '重建索引' }).click();
  await page.getByRole('button', { name: '确认重建', exact: true }).click();
  await expect(row.getByText('等待处理', { exact: true })).toBeVisible();
  await process(request);
  await page.getByRole('button', { name: '刷新文档' }).click();
  await expect(row.getByText('已就绪', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/phase9c-project.png', fullPage: true });
  await row.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(row.getByText('等待删除', { exact: true })).toBeVisible();
  await process(request);
  await page.getByRole('button', { name: '刷新文档' }).click();
  await expect(page.getByText('暂无 PDF 文档。')).toBeVisible();
  const deleted = await request.get(`/api/workbench/projects/${projectId}/documents/${record.id}`, { headers: authorization });
  expect(deleted.status()).toBe(404);
});

test('actual PDF parse failure is visible and confirmed retry returns the same permanent failure', async ({ page, request }) => {
  await connect(page);
  await page.getByRole('link', { name: '中文研究项目' }).click();
  const pdf = await request.get(fixtureURL + '/__fixture/pdf?blank=true');
  await upload(page, await pdf.body(), 'scan.pdf', '无文字 PDF');
  await process(request);
  await page.getByRole('button', { name: '刷新文档' }).click();
  const row = page.getByRole('row').filter({ hasText: '无文字 PDF' });
  await expect(row.getByText('处理失败')).toBeVisible();
  await expect(row.getByText('pdf_text_not_extractable')).toBeVisible();
  await row.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByRole('button', { name: '确认重试', exact: true }).click();
  await expect(row.getByText('等待处理', { exact: true })).toBeVisible();
  await process(request);
  await page.getByRole('button', { name: '刷新文档' }).click();
  await expect(row.getByText('处理失败')).toBeVisible();
});

test('lost acknowledgement after a real committed task is recovered with the same identity and no execution', async ({ page, request }) => {
  await connect(page);
  await page.getByRole('link', { name: '中文研究项目' }).click();
  await page.getByRole('button', { name: '创建研究任务' }).click();
  await page.getByLabel('研究目标', { exact: true }).fill('公开 MNIST HTTP 幂等验收，不执行模型');
  await page.getByLabel('总 Token 预算').fill('100');
  await page.getByRole('checkbox', { name: consent }).check();
  let firstKey: string | undefined;
  let taskId: string | undefined;
  const interrupted = async (route: import('@playwright/test').Route) => {
    firstKey = route.request().headers()['idempotency-key'];
    const response = await route.fetch({ maxRetries: 0, maxRedirects: 0 });
    expect(response.status()).toBe(202);
    taskId = (await response.json()).task_id;
    await route.abort();
  };
  await page.route('**/api/workbench/agent/tasks', interrupted);
  await page.getByRole('button', { name: '确认提交并自动执行' }).click();
  await expect(page.getByRole('button', { name: '用原编号确认 / 重试' })).toBeVisible();
  expect(firstKey).toBeTruthy();
  expect(taskId).toBeTruthy();
  await expect(page.getByLabel('研究目标', { exact: true })).toBeDisabled();
  const committed = await request.get(fixtureURL + '/__fixture/status');
  expect((await committed.json()).task_jobs).toBe(1);
  await page.unroute('**/api/workbench/agent/tasks', interrupted);
  const acknowledgement = page.waitForResponse((response) => response.url().endsWith('/api/workbench/agent/tasks') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '用原编号确认 / 重试' }).click();
  const receipt = await acknowledgement;
  expect(receipt.request().headers()['idempotency-key']).toBe(firstKey);
  expect(await receipt.json()).toMatchObject({ task_id: taskId, created: false });
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  await expect(page.getByText('已排队', { exact: true })).toBeVisible();
  const state = await request.get(fixtureURL + '/__fixture/status');
  expect(await state.json()).toMatchObject({ task_jobs: 1, model_calls: 0, sandbox_calls: 0 });
  await page.screenshot({ path: 'test-results/phase9c-task.png', fullPage: true });
});
