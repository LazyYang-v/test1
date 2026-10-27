export const API_BASE = '/api/v1';

export type ApiProblem = {
  status: number;
  code: string;
  message: string;
  retryable: boolean;
  requestId?: string;
  retryAfter?: string | null;
};

export type Principal = {
  id: string;
  email: string;
  display_name: string;
  regions: Array<{ region_code: string; permission: string }>;
};

export type TaskSummary = {
  task_id: string;
  status: string;
  task_type: string;
  created_at: string;
  updated_at?: string;
  version?: number;
};

export type Metric = {
  metric: string;
  metric_version: number;
  unit: string;
  current_value: string;
  previous_value: string;
  absolute_change: string;
  change_rate: string | null;
  evidence_id: string;
  data_snapshot_id: string;
};

export type Report = {
  summary: string;
  metrics: Metric[];
  findings: Array<{ text: string; evidence_ids: string[] }>;
  hypotheses: Array<{ text: string; status: 'unverified' }>;
  recommendations: string[];
  limitations: string[];
  evidence_ids: string[];
  report_version: string;
};

export type TaskDetail = {
  task_id: string;
  task_type: string;
  status: string;
  version: number;
  current_attempt_no: number | null;
  progress: { current_step: string | null; completed_steps: number; total_steps: number };
  input: {
    question: string;
    region_codes: string[];
    date_range: { start: string; end_exclusive: string; timezone: string };
    compare_mode: string;
  } | null;
  result: Report | null;
  error: { code: string; message: string } | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type TaskEvent = {
  sequence: string;
  type: string;
  payload: Record<string, unknown> | string | null;
  receivedAt: string;
};

async function readProblem(response: Response): Promise<ApiProblem> {
  let body: Record<string, unknown> = {};
  try {
    body = await response.json();
  } catch {
    // Proxies can return plain text when the API is unavailable.
  }
  const error = (body.error ?? {}) as Record<string, unknown>;
  const code = typeof error.code === 'string' ? error.code : `HTTP_${response.status}`;
  const message = localizeError(code, response.status);
  return {
    status: response.status,
    code,
    message,
    retryable: error.retryable === true || response.status === 429 || response.status >= 500,
    requestId: typeof body.request_id === 'string' ? body.request_id : undefined,
    retryAfter: response.headers.get('Retry-After'),
  };
}

const ERROR_MESSAGES: Record<string, string> = {
  UNAUTHENTICATED: '邮箱或密码错误，或账号已停用。',
  INVALID_REQUEST: '提交信息有误，请检查后重试。',
  REGION_ACCESS_DENIED: '当前账号没有访问该业务区域的权限。',
  TASK_NOT_FOUND: '找不到这项任务，或你没有查看权限。',
  IDEMPOTENCY_CONFLICT: '该请求编号已用于其他请求，请刷新后重试。',
  IDEMPOTENCY_RETRY: '任务暂时无法创建，请稍后重试。',
  DATA_SNAPSHOT_UNAVAILABLE: '当前业务数据暂不可用，请稍后重试。',
  OPERATOR_PERMISSION_REQUIRED: '当前账号没有管理员权限。',
  OPERATOR_NOT_FOUND: '找不到该管理员账号。',
  OPERATOR_NOT_ACTIVE: '该管理员账号已停用。',
  OPERATOR_STATE_CONFLICT: '管理员账号状态已变更，请刷新后重试。',
  EMAIL_ALREADY_EXISTS: '该邮箱已注册，请使用其他邮箱。',
  BOOTSTRAP_UNAVAILABLE: '初始化密钥无效或已不可用。',
  BOOTSTRAP_EXPIRED: '初始化密钥已过期，请重新生成。',
  BOOTSTRAP_CONSUMED: '初始化密钥已使用，不能重复使用。',
  BOOTSTRAP_ALREADY_INITIALIZED: '管理员账号已初始化。',
  RECOVERY_UNAVAILABLE: '管理员恢复密钥无效或已不可用。',
  RECOVERY_EXPIRED: '管理员恢复密钥已过期。',
  RECOVERY_CONSUMED: '管理员恢复密钥已使用。',
  MODEL_TIMEOUT: '模型响应超时，任务稍后会自动重试。',
  MODEL_PROVIDER_TIMEOUT: '模型服务响应超时，任务稍后会自动重试。',
  MODEL_PROVIDER_UNAVAILABLE: '模型服务暂不可用，任务稍后会自动重试。',
  MODEL_PROVIDER_5XX: '模型服务遇到错误，任务稍后会自动重试。',
  MODEL_PROVIDER_RATE_LIMITED: '模型服务请求过于频繁，任务稍后会自动重试。',
  MODEL_OUTPUT_INVALID: '模型返回的内容未通过校验，任务无法完成。',
  QUERY_PLAN_INVALID: '分析计划未通过校验，任务无法继续。',
  TOOL_NOT_BOUND: '此 Agent 未配置所需工具，任务无法继续。',
  TOOL_INPUT_INVALID: '工具输入未通过校验，任务无法继续。',
  TOOL_OUTPUT_INVALID: '工具返回的结果未通过校验，任务无法继续。',
  BUDGET_EXCEEDED: '任务已达到调用预算上限。',
  DEADLINE_EXCEEDED: '任务已超过允许的执行时间。',
  ATTEMPTS_EXHAUSTED: '任务重试次数已用完，请稍后重新提交。',
  NETWORK_ERROR: '无法连接到 AgentForge 服务，请检查服务状态后重试。',
  INTERNAL_ERROR: '服务遇到问题，请稍后重试。',
};

const STATUS_MESSAGES: Record<number, string> = {
  400: '提交信息有误，请检查后重试。',
  401: '登录信息无效或已过期，请重新登录。',
  403: '当前账号没有执行此操作的权限。',
  404: '找不到请求的内容，或你没有查看权限。',
  409: '请求与当前任务状态冲突，请刷新后重试。',
  422: '提交内容不符合要求，请检查后重试。',
  429: '操作过于频繁，请稍后重试。',
  500: '服务遇到问题，请稍后重试。',
  502: '服务暂时不可用，请稍后重试。',
  503: '服务暂时不可用，请稍后重试。',
};

export function localizeError(code: string, status?: number): string {
  if (ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  if (code.startsWith('REPORT_')) return '分析报告未通过校验，任务无法完成。';
  return (status === undefined ? undefined : STATUS_MESSAGES[status])
    ?? `请求失败（${code}），请稍后重试或联系管理员。`;
}

export class ApiRequestError extends Error {
  constructor(readonly problem: ApiProblem) {
    super(problem.message);
    this.name = 'ApiRequestError';
  }
}

export async function apiRequest<T>(
  path: string,
  token: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      Authorization: `Bearer ${token}`,
      ...init.headers,
    },
  });
  if (!response.ok) throw new ApiRequestError(await readProblem(response));
  return response.json() as Promise<T>;
}

export async function signIn(email: string, password: string): Promise<string> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw new ApiRequestError(await readProblem(response));
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
}

function parseFrame(frame: string): { id: string; type: string; data: string } | null {
  let id = '';
  let type = 'message';
  const data: string[] = [];
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'id' && !value.includes('\0')) id = value;
    if (field === 'event') type = value || 'message';
    if (field === 'data') data.push(value);
  }
  return id ? { id, type, data: data.join('\n') } : null;
}

/**
 * Native EventSource cannot attach the API's Bearer Authorization header.
 * Keep the JWT out of URLs: an authenticated fetch stream lets us send the
 * header and resume with Last-Event-ID without changing the API's auth model.
 */
export async function streamTaskEvents(
  taskId: string,
  token: string,
  signal: AbortSignal,
  onEvent: (event: TaskEvent) => void,
  onProblem: (problem: ApiProblem) => void,
  onState: (state: 'connecting' | 'live' | 'reconnecting') => void,
): Promise<void> {
  let cursor = '0';
  let retryDelay = 1000;

  while (!signal.aborted) {
    onState(cursor === '0' ? 'connecting' : 'reconnecting');
    try {
      const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/events`, {
        headers: {
          Accept: 'text/event-stream',
          Authorization: `Bearer ${token}`,
          ...(cursor !== '0' ? { 'Last-Event-ID': cursor } : {}),
        },
        signal,
      });
      if (!response.ok) {
        onProblem(await readProblem(response));
        if (response.status === 401 || response.status === 403 || response.status === 404) return;
        throw new Error(`Event stream returned ${response.status}`);
      }
      if (!response.body) throw new Error('Event stream response has no body');

      onState('live');
      retryDelay = 1000;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      try {
        while (!signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
          while (boundary?.index !== undefined) {
            const frame = parseFrame(buffer.slice(0, boundary.index).replace(/\r\n|\r/g, '\n'));
            buffer = buffer.slice(boundary.index + boundary[0].length);
            if (frame) {
              const sequence = frame.id;
              const sequenceDigits = /^\d{1,19}$/.test(sequence);
              const isNewer = sequenceDigits && (sequence.length > cursor.length || (sequence.length === cursor.length && sequence > cursor));
              if (isNewer) {
                let payload: TaskEvent['payload'] = null;
                try {
                  payload = JSON.parse(frame.data) as TaskEvent['payload'];
                } catch {
                  payload = frame.data;
                }
                cursor = sequence;
                onEvent({ sequence, type: frame.type, payload, receivedAt: new Date().toISOString() });
              }
            }
            boundary = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
          }
        }
      } finally {
        reader.releaseLock();
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiRequestError) onProblem(error.problem);
    }

    if (!signal.aborted) {
      onState('reconnecting');
      await new Promise<void>((resolve) => {
        const timeout = window.setTimeout(resolve, retryDelay);
        signal.addEventListener('abort', () => {
          window.clearTimeout(timeout);
          resolve();
        }, { once: true });
      });
      retryDelay = Math.min(retryDelay * 2, 8000);
    }
  }
}
