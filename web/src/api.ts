import type { Overview, Page, Project, Session, Task, TaskStatus } from './types';
import { isOverview, isPage, isProject, isSession, isTask } from './guards';

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
  request_validation_error: '查询参数不合法。',
};
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

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    if (!this.#token) throw new ApiError('experiment_auth_required', 401);
    const pathname = path.split('?')[0];
    if (!/^\/(?:[a-zA-Z0-9_?=&%./+-])*$/.test(path) || pathname.startsWith('//')
      || pathname.includes('..') || /%2f|%5c|%2e/i.test(pathname)) throw new ApiError('invalid_response');
    let response: Response;
    try {
      const timeout = AbortSignal.timeout(15000);
      response = await fetch('/api/workbench' + path, {
        method: 'GET', headers: { Authorization: 'Bearer ' + this.#token, Accept: 'application/json' },
        cache: 'no-store', credentials: 'omit', redirect: 'error',
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch {
      if (signal?.aborted) throw signal.reason;
      throw new ApiError('network_unavailable');
    }
    if (response.status === 401 || response.status === 403) {
      this.clear();
      this.unauthorized();
      throw new ApiError('experiment_auth_required', response.status,
                         response.headers.get('x-request-id'));
    }
    const payload = await readJSON(response);
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
