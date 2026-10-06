import type { Overview, Page, PDFDocument, Project, Session, SubmissionReceipt, Task, TaskStatus, TaskSubmission } from './types';
import { isDocument, isOverview, isPage, isProject, isReceipt, isSession, isTask } from './guards';

export class ApiError extends Error {
  constructor(public code: string, public status = 0, public requestId: string | null = null) {
    super(code);
    this.name = 'ApiError';
  }
}

const messages: Record<string, string> = {
  experiment_auth_required: '操作者 Token 无效，请重新连接。',
  experiment_api_not_configured: '服务端尚未配置 EXPERIMENT_API_TOKEN。',
  project_not_found: '项目不存在。',
  workbench_task_not_found: '根任务不存在，或链接指向了子任务。',
  workbench_store_unavailable: '任务存储暂不可用。',
  network_unavailable: '无法连接本地 API，请检查服务是否启动。',
  invalid_response: 'API 返回格式异常，请检查后端是否已升级。',
  response_too_large: '响应超过安全大小上限。',
  request_validation_error: '输入或查询参数不合法。',
  documents_selection_required: '请选择 1–100 个已就绪 PDF。',
  write_result_unknown: '请求结果未确认，请先核查；不会自动重试。',
  duplicate_document: '该 PDF 已存在于此项目，请刷新文档列表。',
  invalid_pdf: '文件内容不是 PDF。',
  unsupported_document_type: '只支持 PDF 文件。',
  document_too_large: 'PDF 超过允许的大小。',
  document_not_found: '文档不存在，或不属于当前项目。',
  document_not_failed: '文档状态已改变，只有失败文档能重试。',
  document_not_ready: '文档状态已改变，只有就绪文档能重建索引。',
  selected_documents_unavailable: '所选文档必须已就绪并属于当前项目，请刷新后重新选择。',
  idempotency_conflict: '提交编号已对应另一份请求，请核查原任务。',
  workflow_unavailable: '服务端持久化任务服务暂不可用。',
};
export function isWriteUncertain(error: unknown): boolean {
  return error instanceof ApiError && (error.code === 'write_result_unknown' || error.status >= 500);
}
export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (messages[error.code] ?? `请求失败（${error.status || '网络'}）。`)
    : '请求失败，请稍后手动刷新。';
}

async function readJSON(response: Response): Promise<unknown> {
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    throw new ApiError('invalid_response', response.status);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError('invalid_response', response.status);
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let size = 0;
  let text = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 4 * 1024 * 1024) {
        await reader.cancel();
        throw new ApiError('response_too_large', response.status);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('invalid_response', response.status);
  } finally { reader.releaseLock(); }
}

export class ApiClient {
  #token: string;
  constructor(token: string, private unauthorized: () => void = () => {}) {
    if (!/^[\x21-\x7e]{1,4096}$/.test(token)) throw new ApiError('experiment_auth_required', 401);
    this.#token = token;
  }
  clear(): void { this.#token = ''; }
  get connected(): boolean { return this.#token.length > 0; }

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request(path, { signal });
  }
  private async request<T>(path: string, options: {
    method?: 'GET' | 'POST' | 'DELETE'; body?: Record<string, unknown> | FormData;
    key?: string; signal?: AbortSignal; timeout?: number; empty?: boolean;
  } = {}): Promise<T> {
    if (!this.#token) throw new ApiError('experiment_auth_required', 401);
    const { method = 'GET', body, key, signal, timeout = 15000, empty = false } = options;
    if (key !== undefined && !/^[A-Za-z0-9._:-]{1,128}$/.test(key)) throw new ApiError('request_validation_error', 422);
    const pathname = path.split('?')[0];
    if (!/^\/(?:[a-zA-Z0-9_?=&%./+-])*$/.test(path) || pathname.startsWith('//')
      || pathname.includes('..') || /%2f|%5c|%2e/i.test(pathname)) throw new ApiError('invalid_response');
    let response: Response;
    try {
      const deadline = AbortSignal.timeout(timeout);
      const headers: Record<string, string> = { Authorization: 'Bearer ' + this.#token, Accept: 'application/json' };
      const multipart = body instanceof FormData;
      if (body && !multipart) headers['Content-Type'] = 'application/json';
      if (key) headers['Idempotency-Key'] = key;
      response = await fetch('/api/workbench' + path, {
        method, headers, body: body ? (multipart ? body : JSON.stringify(body)) : undefined,
        cache: 'no-store', credentials: 'omit', redirect: 'error',
        signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
      });
    } catch {
      if (signal?.aborted) throw signal.reason;
      throw new ApiError(method === 'GET' ? 'network_unavailable' : 'write_result_unknown');
    }
    if (response.status === 401 || response.status === 403) {
      this.clear();
      this.unauthorized();
      throw new ApiError('experiment_auth_required', response.status,
                         response.headers.get('x-request-id'));
    }
    if (!this.connected) throw new ApiError('experiment_auth_required', 401);
    if (response.ok && empty) {
      await response.body?.cancel();
      return undefined as T;
    }
    let payload: unknown;
    try { payload = await readJSON(response); }
    catch (error) {
      if (method !== 'GET' && (response.ok || response.status >= 500)) {
        throw new ApiError('write_result_unknown', response.status);
      }
      throw error;
    }
    if (!response.ok) {
      const code = (payload as { error?: { code?: unknown } })?.error?.code;
      throw new ApiError(typeof code === 'string' ? code : 'request_failed', response.status,
                         response.headers.get('x-request-id'));
    }
    return payload as T;
  }
  async session(): Promise<Session> {
    return this.checked('/session', isSession);
  }
  private async checked<T>(path: string, validate: (value: unknown) => value is T, signal?: AbortSignal): Promise<T> {
    const value = await this.get<unknown>(path, signal);
    if (!validate(value)) throw new ApiError('invalid_response');
    return value;
  }
  private async write<T>(path: string, validate: (value: unknown) => value is T,
                         body?: Record<string, unknown> | FormData, key?: string): Promise<T> {
    const value = await this.request<unknown>(path, { method: 'POST', body, key, timeout: body instanceof FormData ? 120000 : 15000 });
    if (!validate(value)) throw new ApiError('write_result_unknown', 200);
    return value;
  }
  createProject(name: string, description: string): Promise<Project> {
    return this.write('/projects', isProject, { name: name.trim(), description: description.trim() || null });
  }
  documents(projectId: string, page: number, signal?: AbortSignal): Promise<Page<PDFDocument>> {
    return this.checked(`/projects/${encodeURIComponent(projectId)}/documents?limit=20&offset=${(page - 1) * 20}`,
      (value) => isPage(value, (item): item is PDFDocument => isDocument(item) && item.project_id === projectId), signal);
  }
  async uploadPDF(projectId: string, file: File, title: string, maxBytes: number): Promise<PDFDocument> {
    if (!file.name.toLowerCase().endsWith('.pdf') || !['', 'application/pdf', 'application/x-pdf', 'application/octet-stream'].includes(file.type)) {
      throw new ApiError('unsupported_document_type', 415);
    }
    if (file.size > Math.min(maxBytes, 52428800)) throw new ApiError('document_too_large', 422);
    if (new TextDecoder().decode(await file.slice(0, 5).arrayBuffer()) !== '%PDF-') throw new ApiError('invalid_pdf', 422);
    const data = new FormData();
    data.append('file', file);
    if (title.trim()) data.append('title', title.trim());
    return this.write(`/projects/${encodeURIComponent(projectId)}/documents`, isDocument, data);
  }
  documentAction(projectId: string, documentId: string, action: 'retry' | 'reindex'): Promise<PDFDocument> {
    return this.write(`/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}/${action}`, isDocument);
  }
  deleteDocument(projectId: string, documentId: string): Promise<void> {
    return this.request(`/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`,
      { method: 'DELETE', empty: true });
  }
  submitTask(payload: TaskSubmission, key: string): Promise<SubmissionReceipt> {
    return this.write('/agent/tasks', isReceipt, { ...payload }, key);
  }
  projects(page: number, signal?: AbortSignal): Promise<Page<Project>> {
    return this.checked(`/projects?limit=20&offset=${(page - 1) * 20}`, (value) => isPage(value, isProject), signal);
  }
  project(id: string, signal?: AbortSignal): Promise<Project> {
    return this.checked('/projects/' + encodeURIComponent(id), isProject, signal);
  }
  tasks(projectId: string, page: number, status?: TaskStatus, query?: string,
        signal?: AbortSignal): Promise<Page<Task>> {
    const params = new URLSearchParams({ limit: '20', offset: String((page - 1) * 20) });
    if (status) params.set('status', status);
    if (query) params.set('q', query);
    return this.checked(`/projects/${encodeURIComponent(projectId)}/tasks?${params}`, (value) => isPage(value, isTask), signal);
  }
  overview(id: string, page: number, signal?: AbortSignal): Promise<Overview> {
    return this.checked(`/tasks/${encodeURIComponent(id)}/overview?limit=20&offset=${(page - 1) * 20}`, isOverview, signal);
  }
}
