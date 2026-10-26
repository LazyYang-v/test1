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
  const statusMessages: Record<number, string> = {
    400: 'Check the request details and try again.',
    401: 'Your sign-in has expired. Sign in again to continue.',
    403: 'Your account does not have access to this region or task.',
    404: 'This task could not be found. It may have been removed or the link may be incorrect.',
    409: 'This request conflicts with the task’s current state. Refresh the task and try again.',
    429: 'Too many requests. Wait briefly, then try again.',
  };
  return {
    status: response.status,
    code: typeof error.code === 'string' ? error.code : `HTTP_${response.status}`,
    message:
      typeof error.message === 'string'
        ? error.message
        : statusMessages[response.status] ?? `The API returned status ${response.status}.`,
    retryable: error.retryable === true || response.status === 429 || response.status >= 500,
    requestId: typeof body.request_id === 'string' ? body.request_id : undefined,
    retryAfter: response.headers.get('Retry-After'),
  };
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
