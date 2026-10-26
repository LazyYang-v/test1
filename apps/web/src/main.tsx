import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  ArrowDownLeft,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCircle2,
  CircleAlert,
  Command,
  FileCheck2,
  FileText,
  Gauge,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Plus,
  Radio,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  Waves,
  X,
} from 'lucide-react';
import {
  apiRequest,
  ApiRequestError,
  type ApiProblem,
  type Principal,
  type Report,
  type TaskDetail,
  type TaskEvent,
  type TaskSummary,
  signIn,
  streamTaskEvents,
} from './api';
import './styles.css';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const EVENT_LABELS: Record<string, string> = {
  'task.created': 'Task created',
  'task.attempt.started': 'Analysis started',
  'step.completed': 'Analysis step completed',
  'step.failed': 'Analysis step needs attention',
  'task.completed': 'Analysis complete',
  'task.failed': 'Analysis ended with an error',
};

function problemFrom(error: unknown): ApiProblem {
  if (error instanceof ApiRequestError) return error.problem;
  return {
    status: 0,
    code: 'NETWORK_ERROR',
    message: error instanceof Error ? error.message : 'Could not reach the AgentForge API.',
    retryable: true,
  };
}

function friendlyStatus(status: string): string {
  const labels: Record<string, string> = {
    queued: 'Queued', running: 'In progress', recovering: 'Recovering', retry_wait: 'Retrying',
    cancelling: 'Cancelling', completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled',
  };
  return labels[status] ?? status.replaceAll('_', ' ');
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`}><span className="status-dot" />{friendlyStatus(status)}</span>;
}

function ErrorNotice({ problem, onDismiss }: { problem: ApiProblem; onDismiss?: () => void }) {
  const guidance = problem.status === 429 && problem.retryAfter
    ? `Try again in ${problem.retryAfter} seconds.`
    : problem.retryable
      ? 'Please try again shortly.'
      : problem.status === 409
        ? 'Refresh the task to see its latest status before retrying.'
        : undefined;
  return (
    <div className="error-notice" role="alert">
      <CircleAlert size={18} aria-hidden="true" />
      <div className="error-copy">
        <strong>{problem.message}</strong>
        <span>{problem.code}{problem.requestId ? ` · Request ${problem.requestId}` : ''}{guidance ? ` · ${guidance}` : ''}</span>
      </div>
      {onDismiss && <button className="icon-button error-dismiss" onClick={onDismiss} aria-label="Dismiss message"><X size={16} /></button>}
    </div>
  );
}

function SignIn({ onSignedIn }: { onSignedIn: (token: string, user: Principal) => void }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiProblem | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const token = await signIn(email.trim(), password);
      const user = await apiRequest<Principal>('/auth/me', token);
      onSignedIn(token, user);
    } catch (error) {
      setProblem(problemFrom(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <div className="login-left">
        <div className="brand-lockup"><span className="brand-mark"><Command size={20} strokeWidth={2.4} /></span><span>agentforge<span className="brand-period">.</span></span></div>
        <div className="login-story">
          <div className="eyebrow"><span className="eyebrow-line" />THE ANALYSIS WORKSPACE</div>
          <h1>Business insight,<br /><span>built on evidence.</span></h1>
          <p>Turn a focused business question into a traceable analysis. Every metric, event and recommendation stays connected to its source.</p>
          <div className="login-proof"><span><ShieldCheck size={15} /> Evidence-linked reporting</span><span><Waves size={15} /> Live task updates</span></div>
        </div>
        <div className="login-footer">AGENTFORGE <span>·</span> PRIVATE WORKSPACE</div>
      </div>
      <div className="login-right">
        <div className="login-card-wrap">
          <div className="mobile-brand"><span className="brand-mark"><Command size={18} /></span>agentforge<span className="brand-period">.</span></div>
          <div className="login-heading"><div className="login-icon"><KeyRound size={19} /></div><div><span className="kicker">WELCOME BACK</span><h2>Sign in to continue</h2></div></div>
          <p className="login-subtitle">Use your AgentForge account to open your analysis workspace.</p>
          {problem && <ErrorNotice problem={problem} />}
          <form className="login-form" onSubmit={submit}>
            <label htmlFor="email">Work email</label>
            <input id="email" type="email" autoComplete="username" placeholder="you@company.com" value={email} onChange={(event) => setEmail(event.target.value)} required />
            <div className="password-label"><label htmlFor="password">Password</label></div>
            <input id="password" type="password" autoComplete="current-password" placeholder="Enter your password" value={password} onChange={(event) => setPassword(event.target.value)} required />
            <button className="primary-button login-submit" type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <LockKeyhole size={16} />}{busy ? 'Signing in…' : 'Sign in securely'}<ArrowRight size={16} className="button-arrow" /></button>
          </form>
          <div className="login-security"><LockKeyhole size={13} /> Your connection is secured with short-lived access tokens.</div>
        </div>
        <div className="login-bottom">Need access? Contact your workspace administrator.</div>
      </div>
    </main>
  );
}

function relativeDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('agentforge.accessToken') ?? '');
  const tokenRef = useRef(token);
  const [user, setUser] = useState<Principal | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'reconnecting'>('connecting');
  const [problem, setProblem] = useState<ApiProblem | null>(null);
  const [screen, setScreen] = useState<'overview' | 'create'>('overview');
  const [loading, setLoading] = useState(false);

  const logout = useCallback(() => {
    sessionStorage.removeItem('agentforge.accessToken');
    tokenRef.current = '';
    setToken('');
    setUser(null);
    setTasks([]);
    setSelectedId(null);
    setDetail(null);
    setEvents([]);
    setProblem(null);
  }, []);

  const loadTasks = useCallback(async (accessToken: string) => {
    const response = await apiRequest<{ items: TaskSummary[] }>('/tasks?limit=50', accessToken);
    setTasks(response.items);
  }, []);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    setLoading(true);
    Promise.all([
      apiRequest<Principal>('/auth/me', token),
      apiRequest<{ items: TaskSummary[] }>('/tasks?limit=50', token),
    ]).then(([profile, response]) => {
      if (!alive || tokenRef.current !== token) return;
      setUser(profile);
      setTasks(response.items);
      setProblem(null);
    }).catch((error: unknown) => {
      if (!alive) return;
      const next = problemFrom(error);
      if (next.status === 401) logout();
      else setProblem(next);
    }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [token, logout]);

  useEffect(() => {
    if (!token || !selectedId) return;
    const controller = new AbortController();
    let alive = true;
    setEvents([]);
    const refresh = async () => {
      try {
        const current = await apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(selectedId)}`, token);
        if (alive && tokenRef.current === token) setDetail(current);
      } catch (error) {
        if (!alive || tokenRef.current !== token) return;
        const next = problemFrom(error);
        if (next.status === 401) logout();
        else setProblem(next);
      }
    };
    void refresh();
    const poll = window.setInterval(() => { void refresh(); }, 7000);
    void streamTaskEvents(
      selectedId,
      token,
      controller.signal,
      (event) => {
        setEvents((existing) => {
          if (existing.some((item) => item.sequence === event.sequence)) return existing;
          return [...existing, event].sort((left, right) => left.sequence.length - right.sequence.length || left.sequence.localeCompare(right.sequence)).slice(-80);
        });
        void refresh();
        void loadTasks(token).catch((error: unknown) => setProblem(problemFrom(error)));
      },
      (next) => {
        if (tokenRef.current !== token) return;
        setProblem(next);
        if (next.status === 401) logout();
      },
      setStreamState,
    );
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(poll);
    };
  }, [token, selectedId, loadTasks, logout]);

  const counts = useMemo(() => ({
    all: tasks.length,
    active: tasks.filter((task) => !TERMINAL.has(task.status)).length,
    completed: tasks.filter((task) => task.status === 'completed').length,
  }), [tasks]);

  function signedIn(accessToken: string, profile: Principal) {
    sessionStorage.setItem('agentforge.accessToken', accessToken);
    tokenRef.current = accessToken;
    setToken(accessToken);
    setUser(profile);
    setScreen('overview');
  }

  function openTask(taskId: string) {
    setSelectedId(taskId);
    setDetail(null);
    setEvents([]);
    setProblem(null);
    setScreen('overview');
  }

  if (!token || !user) return <SignIn onSignedIn={signedIn} />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-mark"><Command size={19} strokeWidth={2.4} /></span><span>agentforge<span className="brand-period">.</span></span></div>
        <div className="workspace-switcher"><span className="workspace-avatar">{user.display_name.slice(0, 1).toUpperCase()}</span><span className="workspace-copy"><strong>{user.display_name}</strong><small>Business workspace</small></span><ArrowDownLeft size={14} className="workspace-chevron" /></div>
        <div className="nav-label">WORKSPACE</div>
        <button className={`nav-item ${screen === 'overview' ? 'nav-active' : ''}`} onClick={() => { setScreen('overview'); setSelectedId(null); setDetail(null); }}><Gauge size={17} />Analysis runs<span className="nav-count">{counts.all}</span></button>
        <button className={`nav-item ${screen === 'create' ? 'nav-active' : ''}`} onClick={() => { setScreen('create'); setSelectedId(null); setDetail(null); }}><Plus size={17} />New analysis</button>
        <div className="sidebar-bottom"><div className="sidebar-status"><span className="online-dot" /><span><strong>Platform operational</strong><small>Events are stored durably</small></span></div><button className="nav-item logout-button" onClick={logout}><LogOut size={16} />Sign out</button></div>
      </aside>

      <main className="main-shell">
        <header className="topbar"><div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>{selectedId ? 'Task details' : screen === 'create' ? 'New analysis' : 'Analysis runs'}</strong></div><div className="topbar-right"><span className="environment-label"><span className="online-dot" />LOCAL WORKSPACE</span><span className="topbar-divider" /><button className="profile-button" title={user.email}>{user.display_name.slice(0, 1).toUpperCase()}</button></div></header>
        <div className="page-content">
          {problem && <ErrorNotice problem={problem} onDismiss={() => setProblem(null)} />}
          {screen === 'create' && !selectedId && <CreateTask token={token} user={user} onCreated={(taskId) => { setScreen('overview'); void loadTasks(token); openTask(taskId); }} onProblem={setProblem} onCancel={() => setScreen('overview')} />}
          {screen === 'overview' && !selectedId && <Overview tasks={tasks} counts={counts} loading={loading} onNew={() => setScreen('create')} onSelect={openTask} onRefresh={() => { setLoading(true); void loadTasks(token).catch((error: unknown) => setProblem(problemFrom(error))).finally(() => setLoading(false)); }} />}
          {selectedId && <TaskPage taskId={selectedId} detail={detail} events={events} streamState={streamState} loading={!detail} onBack={() => { setSelectedId(null); setDetail(null); setEvents([]); void loadTasks(token); }} onRefresh={() => { void apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(selectedId)}`, token).then(setDetail).catch((error: unknown) => setProblem(problemFrom(error))); }} />}
        </div>
      </main>
    </div>
  );
}

function Overview({ tasks, counts, loading, onNew, onSelect, onRefresh }: {
  tasks: TaskSummary[];
  counts: { all: number; active: number; completed: number };
  loading: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="overview-page">
      <section className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" />YOUR WORKSPACE</div><h1>Analysis runs</h1><p>Follow your business questions from submission to evidence-backed answer.</p></div><button className="primary-button" onClick={onNew}><Plus size={17} />New analysis<ArrowRight size={15} className="button-arrow" /></button></section>
      <section className="overview-meta"><div className="meta-card"><span className="meta-icon meta-icon-blue"><FileText size={17} /></span><div><span className="meta-label">TOTAL ANALYSES</span><strong>{counts.all}</strong></div></div><div className="meta-card"><span className="meta-icon meta-icon-amber"><Activity size={17} /></span><div><span className="meta-label">IN PROGRESS</span><strong>{counts.active}</strong></div></div><div className="meta-card"><span className="meta-icon meta-icon-green"><CheckCircle2 size={17} /></span><div><span className="meta-label">COMPLETED</span><strong>{counts.completed}</strong></div></div><div className="meta-note"><ShieldCheck size={15} /><span>Your reports are scoped to your current region access.</span></div></section>
      <section className="runs-section"><div className="section-heading"><div><span className="kicker">ACTIVITY</span><h2>Recent analyses</h2></div><button className="quiet-button" onClick={onRefresh} aria-label="Refresh analysis list"><RefreshCw size={15} />Refresh</button></div>
        {loading && tasks.length === 0 ? <div className="loading-state"><LoaderCircle size={20} className="spin" />Loading your analyses…</div> : tasks.length === 0 ? <div className="empty-state"><div className="empty-icon"><Sparkles size={22} /></div><h3>Your next answer starts here.</h3><p>Submit a business question to get a live, evidence-backed analysis.</p><button className="primary-button" onClick={onNew}><Plus size={16} />Start an analysis<ArrowRight size={15} className="button-arrow" /></button></div> : <div className="runs-table-wrap"><table className="runs-table"><thead><tr><th>ANALYSIS</th><th>STATUS</th><th>CREATED</th><th aria-label="Open" /></tr></thead><tbody>{tasks.map((task) => <tr key={task.task_id} onClick={() => onSelect(task.task_id)} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') onSelect(task.task_id); }}><td><span className="task-name"><span className="task-row-icon"><FileCheck2 size={16} /></span><span><strong>Business analysis</strong><small>{task.task_id.slice(0, 8)} · {task.task_type.replaceAll('_', ' ')}</small></span></span></td><td><StatusPill status={task.status} /></td><td className="date-cell">{relativeDate(task.created_at)}</td><td><ArrowRight size={16} className="table-arrow" /></td></tr>)}</tbody></table></div>}
      </section>
      <div className="overview-footnote"><LockKeyhole size={13} />Task details and reports are only available to their owner.</div>
    </div>
  );
}

function CreateTask({ token, user, onCreated, onProblem, onCancel }: {
  token: string;
  user: Principal;
  onCreated: (id: string) => void;
  onProblem: (problem: ApiProblem | null) => void;
  onCancel: () => void;
}) {
  const [agentKey, setAgentKey] = useState('sales-report');
  const [region, setRegion] = useState(user.regions[0]?.region_code ?? '');
  const [start, setStart] = useState(() => {
    const date = new Date(); date.setDate(date.getDate() - 7); return date.toISOString().slice(0, 10);
  });
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [compareMode, setCompareMode] = useState('previous_period');
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onProblem(null);
    setBusy(true);
    try {
      const idempotencyKey = crypto.randomUUID();
      const response = await apiRequest<{ task_id: string }>('/tasks', token, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({
          agent_key: agentKey.trim(),
          task_type: 'business_analysis',
          input: {
            question: `Analyze GMV for ${region} from ${start} through ${end} and compare with the previous period.`,
            region_codes: [region],
            date_range: { start, end_exclusive: end, timezone },
            compare_mode: compareMode,
          },
        }),
      });
      onCreated(response.task_id);
    } catch (error) {
      onProblem(problemFrom(error));
      setBusy(false);
    }
  }

  return (
    <div className="create-page"><button className="back-link" onClick={onCancel}><ArrowLeft size={15} />Back to analyses</button><section className="page-intro create-intro"><div><div className="eyebrow"><span className="eyebrow-line" />NEW WORK ORDER</div><h1>Ask a business question.</h1><p>Choose the scope and time period. Your analysis will use only regions you can access.</p></div></section>
      <div className="create-layout"><form className="create-card" onSubmit={submit}><div className="form-section-heading"><span className="step-number">01</span><div><h2>Analysis scope</h2><p>Set the agent and business region for this run.</p></div></div>
        <div className="field-grid"><div className="field-group"><label htmlFor="agent">Agent key</label><input id="agent" value={agentKey} onChange={(event) => setAgentKey(event.target.value)} required maxLength={128} placeholder="sales-report" /><small>Published agent version used for this analysis.</small></div><div className="field-group"><label htmlFor="region">Region</label><select id="region" value={region} onChange={(event) => setRegion(event.target.value)} required>{user.regions.length === 0 && <option value="">No regions available</option>}{user.regions.map((grant) => <option key={grant.region_code} value={grant.region_code}>{grant.region_code} <span>·</span> {grant.permission} access</option>)}</select><small>Only regions granted to your account are available.</small></div></div>
        <div className="form-divider" /><div className="form-section-heading"><span className="step-number">02</span><div><h2>Time period</h2><p>Calendar dates use an inclusive start and exclusive end.</p></div></div>
        <div className="field-grid date-grid"><div className="field-group"><label htmlFor="start-date">Start date <span className="label-note">inclusive</span></label><input id="start-date" type="date" value={start} max={end} onChange={(event) => setStart(event.target.value)} required /></div><div className="date-separator"><ArrowRight size={16} /></div><div className="field-group"><label htmlFor="end-date">End date <span className="label-note">exclusive</span></label><input id="end-date" type="date" value={end} min={start} onChange={(event) => setEnd(event.target.value)} required /></div></div>
        <div className="field-grid lower-grid"><div className="field-group"><label htmlFor="timezone">Time zone</label><input id="timezone" value={timezone} onChange={(event) => setTimezone(event.target.value)} required placeholder="America/Los_Angeles" /></div><div className="field-group"><label htmlFor="compare">Comparison</label><select id="compare" value={compareMode} onChange={(event) => setCompareMode(event.target.value)}><option value="previous_period">Previous period</option></select></div></div>
        <div className="form-summary"><span className="summary-icon"><Sparkles size={17} /></span><span><strong>Ready to analyze</strong><small>{region || 'Choose a region'} · {start} to {end} · compared with the previous period</small></span></div>
        <div className="form-actions"><span><LockKeyhole size={13} />Submitted securely to your workspace</span><button type="submit" className="primary-button" disabled={busy || !region || start >= end}>{busy ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}{busy ? 'Creating…' : 'Create analysis'}<ArrowRight size={15} className="button-arrow" /></button></div>
      </form><aside className="create-aside"><div className="aside-icon"><ShieldCheck size={19} /></div><h3>Built for traceability</h3><p>Every run keeps its metrics, event history and evidence references together.</p><div className="aside-divider" /><div className="aside-feature"><span><Radio size={15} /></span><div><strong>Live progress</strong><small>Watch task events arrive as work proceeds.</small></div></div><div className="aside-feature"><span><FileCheck2 size={15} /></span><div><strong>Verifiable results</strong><small>Metrics include IDs you can trace to evidence.</small></div></div><div className="aside-note"><TriangleAlert size={14} />Analysis can take a few moments to complete.</div></aside></div>
    </div>
  );
}

function TaskPage({ taskId, detail, events, streamState, loading, onBack, onRefresh }: {
  taskId: string;
  detail: TaskDetail | null;
  events: TaskEvent[];
  streamState: 'connecting' | 'live' | 'reconnecting';
  loading: boolean;
  onBack: () => void;
  onRefresh: () => void;
}) {
  return (
    <div className="task-page"><button className="back-link" onClick={onBack}><ArrowLeft size={15} />All analyses</button>{loading && !detail ? <div className="loading-state task-loading"><LoaderCircle size={20} className="spin" />Loading task details…</div> : detail ? <>
      <section className="task-heading"><div><div className="eyebrow"><span className="eyebrow-line" />BUSINESS ANALYSIS <span className="heading-dot">·</span> {taskId.slice(0, 8)}</div><h1>Analysis details</h1><p>Submitted {relativeDate(detail.created_at)}{detail.current_attempt_no ? ` · Attempt ${detail.current_attempt_no}` : ''}</p></div><div className="task-heading-actions"><StatusPill status={detail.status} /><button className="quiet-button" onClick={onRefresh} aria-label="Refresh task"><RefreshCw size={15} />Refresh</button></div></section>
      <div className="task-columns"><section className="task-primary"><div className="panel report-panel"><div className="panel-header"><div><span className="kicker">ANALYSIS REPORT</span><h2>{TERMINAL.has(detail.status) ? detail.status === 'completed' ? 'Your answer is ready' : 'Run summary' : 'Work in progress'}</h2></div>{detail.result && <span className="version-tag">REPORT {detail.result.report_version}</span>}</div>{detail.status === 'completed' && detail.result ? <ReportView report={detail.result} /> : detail.status === 'failed' ? <div className="task-error"><span className="task-error-icon"><TriangleAlert size={18} /></span><div><strong>{detail.error?.code ?? 'TASK_FAILED'}</strong><p>{detail.error?.message ?? 'The analysis could not be completed. Review the event history for more context.'}</p></div></div> : detail.status === 'cancelled' ? <div className="progress-state"><div className="progress-illustration progress-cancelled"><X size={22} /></div><h3>This analysis was cancelled</h3><p>The task reached a terminal state before a report was produced.</p></div> : <div className="progress-state"><div className="progress-illustration"><LoaderCircle size={22} className="spin" /></div><h3>{detail.status === 'queued' ? 'Your analysis is in the queue' : detail.status === 'retry_wait' || detail.status === 'recovering' ? 'Safely resuming your analysis' : 'Your analysis is underway'}</h3><p>Progress updates appear here as the work completes. You can leave this page; events are stored and will be available when you return.</p><div className="progress-steps">{['Plan', 'Measure', 'Explain', 'Validate'].map((step, index) => <span key={step} className={index < detail.progress.completed_steps ? 'step-done' : index === detail.progress.completed_steps && detail.status === 'running' ? 'step-current' : ''}><span>{index < detail.progress.completed_steps ? <Check size={12} /> : index + 1}</span>{step}</span>)}</div></div>}</div>
        {detail.result && <ReportEvidenceFooter report={detail.result} />}
      </section>
      <aside className="task-aside"><div className="panel live-panel"><div className="panel-header"><div><span className="kicker">DURABLE EVENT STREAM</span><h2>Run activity</h2></div><span className={`stream-indicator stream-${streamState}`}><span className="status-dot" />{streamState === 'live' ? 'LIVE' : streamState === 'connecting' ? 'CONNECTING' : 'RECONNECTING'}</span></div><div className="event-list">{events.length === 0 && <div className="event-empty"><LoaderCircle size={16} className="spin" />Waiting for task events…</div>}{[...events].reverse().map((event) => <article className="event-item" key={event.sequence}><span className="event-marker"><span /></span><div><div className="event-title"><strong>{EVENT_LABELS[event.type] ?? event.type.replaceAll('.', ' ').replaceAll('_', ' ')}</strong><span>#{event.sequence}</span></div>{typeof event.payload === 'string' && <p>{event.payload}</p>}<time>{relativeDate(event.receivedAt)}</time></div></article>)}</div><div className="event-panel-footer"><span><ShieldCheck size={13} />Events replay after reconnect</span><span>Deduplicated by sequence</span></div></div>
        <div className="panel task-info"><span className="kicker">TASK INFORMATION</span><dl><div><dt>Task ID</dt><dd className="mono-value">{detail.task_id}</dd></div><div><dt>Type</dt><dd>{detail.task_type.replaceAll('_', ' ')}</dd></div><div><dt>Created</dt><dd>{relativeDate(detail.created_at)}</dd></div><div><dt>Last updated</dt><dd>{relativeDate(detail.updated_at)}</dd></div><div><dt>Current attempt</dt><dd>{detail.current_attempt_no ?? 'Not started'}</dd></div><div><dt>Task version</dt><dd>{detail.version}</dd></div></dl></div>
      </aside></div>
      </> : <div className="empty-state"><div className="empty-icon"><CircleAlert size={22} /></div><h3>Task details unavailable</h3><p>Refresh the task to load its latest state.</p><button className="primary-button" onClick={onRefresh}><RefreshCw size={15} />Retry</button></div>}</div>
  );
}

function money(value: string, unit: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `${value} ${unit}`;
  const formatted = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(parsed);
  return ['currency', 'cny'].includes(unit.toLowerCase()) ? `¥${formatted}` : `${formatted} ${unit}`;
}

function ReportView({ report }: { report: Report }) {
  return <div className="report-content"><p className="report-summary">{report.summary}</p><section className="report-section"><div className="report-section-title"><span className="report-section-icon"><Activity size={15} /></span><div><h3>Verified metrics</h3><p>Calculated from the task’s authorized data scope.</p></div></div>{report.metrics.length ? <div className="metric-table-wrap"><table className="metric-table"><thead><tr><th>METRIC</th><th>CURRENT</th><th>PREVIOUS</th><th>CHANGE</th></tr></thead><tbody>{report.metrics.map((metric) => <tr key={metric.evidence_id}><td><strong>{metric.metric.toUpperCase()}</strong><small>Version {metric.metric_version}</small></td><td>{money(metric.current_value, metric.unit)}</td><td>{money(metric.previous_value, metric.unit)}</td><td className={metric.change_rate && metric.change_rate.startsWith('-') ? 'negative-change' : 'positive-change'}>{metric.change_rate === null ? <span className="null-change">Not defined</span> : `${(Number(metric.change_rate) * 100).toFixed(2)}%`}</td></tr>)}</tbody></table></div> : <p className="report-empty">No metric values were included in this report.</p>}</section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon"><ArrowUpRight size={15} /></span><div><h3>Findings</h3><p>Observations linked to verified evidence.</p></div></div><div className="report-list">{report.findings.map((finding, index) => <article className="report-list-item finding-item" key={`${finding.evidence_ids.join('-')}-${index}`}><span className="list-index">{String(index + 1).padStart(2, '0')}</span><div><p>{finding.text}</p><div className="evidence-chips">{finding.evidence_ids.map((id) => <span className="evidence-chip" key={id}><FileCheck2 size={12} />{id}</span>)}</div></div></article>)}</div></section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon hypothesis-icon"><Sparkles size={15} /></span><div><h3>Hypotheses</h3><p>Possible explanations that need further validation.</p></div></div>{report.hypotheses.length ? <div className="report-list">{report.hypotheses.map((hypothesis, index) => <article className="report-list-item hypothesis-item" key={index}><span className="hypothesis-badge">UNVERIFIED</span><p>{hypothesis.text}</p></article>)}</div> : <p className="report-empty">No hypotheses were included.</p>}</section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon recommendation-icon"><CheckCircle2 size={15} /></span><div><h3>Recommendations</h3><p>Suggested next steps based on this analysis.</p></div></div><ul className="recommendation-list">{report.recommendations.map((item, index) => <li key={index}><span><ArrowRight size={13} /></span>{item}</li>)}</ul></section>
    <section className="report-section limitations-section"><div className="report-section-title"><span className="report-section-icon limitation-icon"><TriangleAlert size={15} /></span><div><h3>Limitations</h3><p>Context to consider when using this report.</p></div></div>{report.limitations.length ? <ul className="limitation-list">{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="report-empty">No limitations were reported.</p>}</section>
  </div>;
}

function ReportEvidenceFooter({ report }: { report: Report }) {
  return <div className="report-footnote"><span><FileText size={14} />{report.evidence_ids.length} evidence reference{report.evidence_ids.length === 1 ? '' : 's'}</span><span>Snapshot-bound · v{report.report_version}</span></div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
