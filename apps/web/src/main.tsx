import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  ArrowDownLeft,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  Ban,
  CalendarDays,
  Check,
  CheckCircle2,
  CircleAlert,
  Command,
  Database,
  Download,
  FileCheck2,
  FileText,
  Gauge,
  GitBranch,
  KeyRound,
  LoaderCircle,
  ListChecks,
  LockKeyhole,
  LogOut,
  Plus,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  TrendingDown,
  TrendingUp,
  Waves,
  X,
} from 'lucide-react';
import {
  apiRequest,
  ApiRequestError,
  type ApiProblem,
  type Metric,
  type Principal,
  type Report,
  type TaskDetail,
  type TaskEvent,
  type TaskSummary,
  localizeError,
  signIn,
  streamTaskEvents,
} from './api';
import './styles.css';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const EVENT_LABELS: Record<string, string> = {
  'task.created': '任务已创建',
  'task.attempt.started': '分析已开始',
  'step.completed': '分析步骤已完成',
  'step.failed': '分析步骤遇到问题',
  'task.completed': '分析已完成',
  'task.failed': '分析失败',
  'task.retry_scheduled': '分析将自动重试',
  'task.recovering': '正在恢复分析',
  'task.requeued': '任务已重新排队',
  'task.cancel.requested': '已提交取消请求',
  'task.cancelling': '正在取消任务',
  'task.cancelled': '任务已取消',
};

const DEFAULT_QUESTION = '分析最近 7 天 GMV 为什么下降了 18%？请基于可用数据列出证据、待验证假设和建议；没有数据支持的原因请明确说明。';
const SAMPLE_START = '2025-03-31';
const SAMPLE_END_EXCLUSIVE = '2025-04-07';
const APPROVALS_STORAGE_KEY = 'agentforge.approvalDrafts.v1';
const QUESTIONS_STORAGE_KEY = 'agentforge.taskQuestions.v1';

type Screen = 'overview' | 'tasks' | 'trace' | 'reports' | 'approvals' | 'create';
type ApprovalStatus = 'pending' | 'approved' | 'changes_requested' | 'rejected';
type ApprovalHistoryItem = { action: string; actor: string; at: string };
type ApprovalProposal = {
  id: string;
  taskId: string;
  title: string;
  summary: string;
  recommendations: string[];
  evidenceIds: string[];
  status: ApprovalStatus;
  createdAt: string;
  history: ApprovalHistoryItem[];
};

function readApprovalDrafts(): Record<string, ApprovalProposal[]> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(APPROVALS_STORAGE_KEY) ?? '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return parsed as Record<string, ApprovalProposal[]>;
  } catch {
    return {};
  }
}

function readStoredQuestions(): Record<string, string> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(QUESTIONS_STORAGE_KEY) ?? '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        (entry): entry is [string, string] => typeof entry[1] === 'string',
      ),
    );
  } catch {
    return {};
  }
}

function problemFrom(error: unknown): ApiProblem {
  if (error instanceof ApiRequestError) return error.problem;
  return {
    status: 0,
    code: 'NETWORK_ERROR',
    message: '无法连接到 AgentForge 服务，请检查服务状态后重试。',
    retryable: true,
  };
}

function friendlyStatus(status: string): string {
  const labels: Record<string, string> = {
    queued: '排队中', running: '运行中', recovering: '恢复中', retry_wait: '等待重试',
    cancelling: '正在取消', completed: '已完成', failed: '失败', cancelled: '已取消',
  };
  return labels[status] ?? '状态更新中';
}

function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? (type.startsWith('step.') ? '分析步骤更新' : '任务进度更新');
}

function eventDetails(event: TaskEvent): string {
  if (typeof event.payload === 'string') return event.payload;
  if (!event.payload || typeof event.payload !== 'object') return '';
  const payload = event.payload;
  const fields = [
    typeof payload.step_key === 'string' ? `步骤 ${payload.step_key}` : '',
    typeof payload.graph_node_key === 'string' ? `Agent 节点 ${payload.graph_node_key}` : '',
    typeof payload.attempt_no === 'number' ? `第 ${payload.attempt_no} 次尝试` : '',
    typeof payload.status === 'string' ? `状态 ${payload.status}` : '',
    typeof payload.reason === 'string' ? payload.reason : '',
  ].filter(Boolean);
  return fields.join(' · ');
}

function taskTypeLabel(type: string): string {
  return type === 'business_analysis' ? '商业分析' : '其他任务';
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`}><span className="status-dot" />{friendlyStatus(status)}</span>;
}

function ErrorNotice({ problem, onDismiss }: { problem: ApiProblem; onDismiss?: () => void }) {
  const guidance = problem.status === 429 && problem.retryAfter
    ? `请在 ${problem.retryAfter} 秒后重试。`
    : problem.retryable
      ? '请稍后重试。'
      : problem.status === 409
        ? '请刷新任务状态后重试。'
        : undefined;
  return (
    <div className="error-notice" role="alert">
      <CircleAlert size={18} aria-hidden="true" />
      <div className="error-copy">
        <strong>{problem.message}</strong>
        <span>{problem.code}{problem.requestId ? ` · 请求编号 ${problem.requestId}` : ''}{guidance ? ` · ${guidance}` : ''}</span>
      </div>
      {onDismiss && <button className="icon-button error-dismiss" onClick={onDismiss} aria-label="关闭提示"><X size={16} /></button>}
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
          <div className="eyebrow"><span className="eyebrow-line" />商业分析工作台</div>
          <h1>让商业洞察，<br /><span>有据可循。</span></h1>
          <p>把业务问题转化为清晰、可追溯的分析。每项指标、事件与建议都有据可查。</p>
          <div className="login-proof"><span><ShieldCheck size={15} /> 证据关联报告</span><span><Waves size={15} /> 任务实时更新</span></div>
        </div>
        <div className="login-footer">AGENTFORGE <span>·</span> 专属工作区</div>
      </div>
      <div className="login-right">
        <div className="login-card-wrap">
          <div className="mobile-brand"><span className="brand-mark"><Command size={18} /></span>agentforge<span className="brand-period">.</span></div>
          <div className="login-heading"><div className="login-icon"><KeyRound size={19} /></div><div><span className="kicker">欢迎回来</span><h2>登录 AgentForge</h2></div></div>
          <p className="login-subtitle">使用你的账号进入商业分析工作台。</p>
          {problem && <ErrorNotice problem={problem} />}
          <form className="login-form" onSubmit={submit}>
            <label htmlFor="email">邮箱</label>
            <input id="email" type="email" autoComplete="username" placeholder="请输入邮箱地址" value={email} onChange={(event) => setEmail(event.target.value)} required />
            <div className="password-label"><label htmlFor="password">密码</label></div>
            <input id="password" type="password" autoComplete="current-password" placeholder="请输入密码" value={password} onChange={(event) => setPassword(event.target.value)} required />
            <button className="primary-button login-submit" type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <LockKeyhole size={16} />}{busy ? '正在登录…' : '安全登录'}<ArrowRight size={16} className="button-arrow" /></button>
          </form>
          <div className="login-security"><LockKeyhole size={13} /> 使用短时有效的访问令牌保护你的连接。</div>
        </div>
        <div className="login-bottom">需要开通账号？请联系工作区管理员。</div>
      </div>
    </main>
  );
}

function relativeDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
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
  const [screen, setScreen] = useState<Screen>('overview');
  const [taskOrigin, setTaskOrigin] = useState<Screen>('tasks');
  const [draftQuestion, setDraftQuestion] = useState(DEFAULT_QUESTION);
  const [taskQuestions, setTaskQuestions] = useState<Record<string, string>>(readStoredQuestions);
  const [approvalsByUser, setApprovalsByUser] = useState(readApprovalDrafts);
  const [loading, setLoading] = useState(false);
  const [latestReport, setLatestReport] = useState<TaskDetail | null>(null);

  useEffect(() => {
    localStorage.setItem(APPROVALS_STORAGE_KEY, JSON.stringify(approvalsByUser));
  }, [approvalsByUser]);

  useEffect(() => {
    localStorage.setItem(QUESTIONS_STORAGE_KEY, JSON.stringify(taskQuestions));
  }, [taskQuestions]);

  const latestCompletedId = useMemo(
    () => tasks.find((task) => task.status === 'completed')?.task_id ?? null,
    [tasks],
  );

  useEffect(() => {
    if (!token || !latestCompletedId) {
      setLatestReport(null);
      return;
    }
    let alive = true;
    void apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(latestCompletedId)}`, token)
      .then((detail) => { if (alive) setLatestReport(detail.result ? detail : null); })
      .catch(() => { /* 概览保留标杆演示卡片 */ });
    return () => { alive = false; };
  }, [token, latestCompletedId]);

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
    setScreen('overview');
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

  const streamEvents = useCallback((taskId: string, detail: TaskDetail) => {
    if (detail.input?.question) {
      setTaskQuestions((current) => current[taskId] === detail.input!.question
        ? current
        : { ...current, [taskId]: detail.input!.question });
    }
  }, []);

  useEffect(() => {
    if (!token || !selectedId) return;
    const controller = new AbortController();
    let alive = true;
    setEvents([]);
    const refresh = async () => {
      try {
        const current = await apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(selectedId)}`, token);
        if (alive && tokenRef.current === token) {
          streamEvents(selectedId, current);
          setDetail(current);
        }
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
  }, [token, selectedId, loadTasks, logout, streamEvents]);

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

  function navigate(next: Screen) {
    setScreen(next);
    setSelectedId(null);
    setDetail(null);
    setEvents([]);
    setProblem(null);
  }

  function openTask(taskId: string, destination: 'trace' | 'reports' = 'trace') {
    setTaskOrigin(screen === 'create' ? 'overview' : screen);
    setSelectedId(taskId);
    setDetail(null);
    setEvents([]);
    setProblem(null);
    setScreen(destination);
  }

  function addApprovalDraft(taskId: string, report: Report) {
    if (!user) return;
    const existing = approvalsByUser[user.id] ?? [];
    if (!existing.some((proposal) => proposal.taskId === taskId && proposal.status === 'pending')) {
      const now = new Date().toISOString();
      const proposal: ApprovalProposal = {
        id: crypto.randomUUID(),
        taskId,
        title: '运营行动方案',
        summary: report.summary,
        recommendations: report.recommendations,
        evidenceIds: report.evidence_ids,
        status: 'pending',
        createdAt: now,
        history: [{ action: '创建审批草案', actor: user.display_name, at: now }],
      };
      setApprovalsByUser((current) => ({ ...current, [user.id]: [proposal, ...(current[user.id] ?? [])] }));
    }
    navigate('approvals');
  }

  function updateApprovalDraft(proposalId: string, status: Exclude<ApprovalStatus, 'pending'>) {
    if (!user) return;
    const labels: Record<typeof status, string> = {
      approved: '演示审批通过',
      changes_requested: '退回修改',
      rejected: '演示审批拒绝',
    };
    const now = new Date().toISOString();
    setApprovalsByUser((current) => ({
      ...current,
      [user.id]: (current[user.id] ?? []).map((proposal) => proposal.id === proposalId
        ? { ...proposal, status, history: [...proposal.history, { action: labels[status], actor: user.display_name, at: now }] }
        : proposal),
    }));
  }

  if (!token || !user) return <SignIn onSignedIn={signedIn} />;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-mark"><Command size={19} strokeWidth={2.4} /></span><span className="brand-name">AgentForge<small>Enterprise</small></span></div>
        <div className="workspace-switcher"><span className="workspace-avatar">{user.display_name.slice(0, 1).toUpperCase()}</span><span className="workspace-copy"><strong>{user.display_name}</strong><small>{user.regions.length} 个授权区域</small></span><ArrowDownLeft size={14} className="workspace-chevron" /></div>
        <div className="nav-label">运营分析</div>
        <nav className="sidebar-nav" aria-label="主导航">
          <button className={`nav-item ${!selectedId && screen === 'overview' ? 'nav-active' : ''}`} onClick={() => navigate('overview')}><Gauge size={17} />运营工作台</button>
          <button className={`nav-item ${!selectedId && screen === 'tasks' ? 'nav-active' : ''}`} onClick={() => navigate('tasks')}><FileText size={17} />分析任务<span className="nav-count">{counts.all}</span></button>
          <button className={`nav-item ${(selectedId && screen === 'trace') || (!selectedId && screen === 'trace') ? 'nav-active' : ''}`} onClick={() => navigate('trace')}><GitBranch size={17} />执行 Trace</button>
          <button className={`nav-item ${(!selectedId && screen === 'reports') || (selectedId && screen === 'reports') ? 'nav-active' : ''}`} onClick={() => navigate('reports')}><FileCheck2 size={17} />分析报告</button>
          <button className={`nav-item ${!selectedId && screen === 'approvals' ? 'nav-active' : ''}`} onClick={() => navigate('approvals')}><ShieldCheck size={17} />审批中心<span className="nav-count">{(approvalsByUser[user.id] ?? []).filter((item) => item.status === 'pending').length}</span></button>
        </nav>
        <div className="sidebar-capability"><span className="capability-icon"><Database size={14} /></span><span><strong>GMV v1 已接入</strong><small>只读指标 · 快照数据</small></span></div>
        <div className="sidebar-bottom"><div className="sidebar-status"><span className="online-dot" /><span><strong>任务运行时已连接</strong><small>事件持久保存并支持恢复</small></span></div><button className="nav-item logout-button" onClick={logout}><LogOut size={16} />退出登录</button></div>
      </aside>

      <main className="main-shell">
        <header className="topbar"><div className="breadcrumbs"><span>星海电商</span><span className="crumb-slash">/</span><strong>{selectedId ? screen === 'reports' ? '分析报告' : '执行 Trace' : ({ overview: '运营工作台', tasks: '分析任务', trace: '执行 Trace', reports: '分析报告', approvals: '审批中心', create: '新建分析' } satisfies Record<Screen, string>)[screen]}</strong></div><div className="topbar-right"><span className="environment-label"><span className="online-dot" />合成数据工作区</span><span className="topbar-divider" /><button className="profile-button" title={user.email} aria-label={`当前账号：${user.email}`}>{user.display_name.slice(0, 1).toUpperCase()}</button></div></header>
        <div className="page-content">
          {problem && <ErrorNotice problem={problem} onDismiss={() => setProblem(null)} />}
          {screen === 'create' && !selectedId && <CreateTask token={token} user={user} initialQuestion={draftQuestion} onQuestionChange={setDraftQuestion} onCreated={(taskId, question) => { setTaskQuestions((current) => ({ ...current, [taskId]: question })); void loadTasks(token); openTask(taskId); }} onProblem={setProblem} onCancel={() => navigate('overview')} />}
          {screen === 'overview' && !selectedId && <Overview tasks={tasks} counts={counts} loading={loading} question={draftQuestion} onQuestionChange={setDraftQuestion} reportTask={latestReport} onNew={() => navigate('create')} onSelect={(id) => openTask(id)} onNavigate={navigate} onRefresh={() => { setLoading(true); void loadTasks(token).catch((error: unknown) => setProblem(problemFrom(error))).finally(() => setLoading(false)); }} />}
          {['tasks', 'trace', 'reports'].includes(screen) && !selectedId && <TaskListPage screen={screen as 'tasks' | 'trace' | 'reports'} tasks={tasks} taskQuestions={taskQuestions} loading={loading} onNew={() => navigate('create')} onSelect={(id) => openTask(id, screen === 'reports' ? 'reports' : 'trace')} onRefresh={() => { setLoading(true); void loadTasks(token).catch((error: unknown) => setProblem(problemFrom(error))).finally(() => setLoading(false)); }} />}
          {screen === 'approvals' && !selectedId && <ApprovalCenter proposals={approvalsByUser[user.id] ?? []} onUpdate={updateApprovalDraft} onOpenTask={(taskId) => openTask(taskId)} />}
          {selectedId && <TaskPage taskId={selectedId} question={taskQuestions[selectedId] ?? detail?.input?.question} detail={detail} events={events} streamState={streamState} loading={!detail} onBack={() => { setSelectedId(null); setDetail(null); setEvents([]); setScreen(taskOrigin); void loadTasks(token); }} onRefresh={() => { void apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(selectedId)}`, token).then(setDetail).catch((error: unknown) => setProblem(problemFrom(error))); }} onCancel={async (task) => { try { await apiRequest(`/tasks/${encodeURIComponent(task.task_id)}/cancel`, token, { method: 'POST', body: JSON.stringify({ reason: '用户从分析工作台取消任务', expected_version: task.version }) }); const latest = await apiRequest<TaskDetail>(`/tasks/${encodeURIComponent(task.task_id)}`, token); setDetail(latest); void loadTasks(token); } catch (error) { setProblem(problemFrom(error)); } }} onCreateApproval={addApprovalDraft} />}
        </div>
      </main>
    </div>
  );
}

function metricBadge(metric: Metric) {
  const rate = Number(metric.change_rate);
  if (!Number.isFinite(rate) || rate === 0) {
    return <span className="flat-badge">持平</span>;
  }
  const percent = Math.abs(rate * 100).toFixed(1);
  return rate < 0
    ? <span className="decline-badge"><TrendingDown size={15} />{percent}%</span>
    : <span className="rise-badge"><TrendingUp size={15} />{percent}%</span>;
}

function Overview({ tasks, counts, loading, question, onQuestionChange, reportTask, onNew, onSelect, onNavigate, onRefresh }: {
  tasks: TaskSummary[];
  counts: { all: number; active: number; completed: number };
  loading: boolean;
  question: string;
  onQuestionChange: (value: string) => void;
  reportTask: TaskDetail | null;
  onNew: () => void;
  onSelect: (id: string) => void;
  onNavigate: (screen: Screen) => void;
  onRefresh: () => void;
}) {
  const recentTasks = tasks.slice(0, 4);
  return (
    <div className="dashboard-page">
      <section className="dashboard-heading">
        <div><div className="eyebrow"><span className="eyebrow-line" />星海电商 · 运营分析</div><h1>把业务问题，变成可核验的决策依据</h1><p>从 GMV 指标开始，查看真实运行记录、证据和未验证假设。</p></div>
        <span className="dataset-badge"><Database size={14} />冻结合成数据快照</span>
      </section>

      <section className="question-composer">
        <div className="composer-title"><span className="composer-icon"><Sparkles size={17} /></span><div><strong>发起一次运营诊断</strong><small>使用已发布的 sales-report Agent 和只读 GMV 指标</small></div></div>
        <textarea aria-label="分析问题" value={question} onChange={(event) => onQuestionChange(event.target.value)} rows={2} maxLength={10000} />
        <div className="composer-footer"><span><CalendarDays size={14} />默认 2025-03-31 至 2025-04-07 · CN_EAST 标杆区间（需有读取权限）</span><button className="primary-button" onClick={onNew} disabled={!question.trim()}><BarChart3 size={16} />配置并开始分析<ArrowRight size={15} className="button-arrow" /></button></div>
      </section>

      <section className="snapshot-note" aria-label="业务数据范围"><span className="snapshot-note-icon"><CircleAlert size={17} /></span><div><strong>数据能力边界</strong><p>当前已接入订单快照中的 GMV v1 指标。库存、退款、流量和营销表尚未接入，报告不会将这些因素写成已验证原因。</p></div></section>

      <section className="dashboard-summary" aria-label="演示数据与平台状态">
        <article className="summary-feature">{(() => {
          const metric = reportTask?.result?.metrics[0];
          const range = reportTask?.input?.date_range;
          return <>
            <div className="summary-feature-top"><span>{metric ? 'GMV 最新诊断' : 'GMV 标杆诊断'}</span><span className="demo-label">{metric ? '来自最新报告' : '合成数据'}</span></div>
            <div className="summary-value-row"><strong>{metric ? money(metric.current_value, metric.unit) : '¥57,400'}</strong>{metric ? metricBadge(metric) : <span className="decline-badge"><TrendingDown size={15} />18.0%</span>}</div>
            <p>{metric && range ? `${range.start} 至 ${range.end_exclusive} · 前一周期 ${money(metric.previous_value, metric.unit)}` : '2025-03-31 至 2025-04-07 · 前一周期 ¥70,000'}</p>
            <div className="summary-mini-chart" aria-hidden="true"><svg viewBox="0 0 520 72" preserveAspectRatio="none"><defs><linearGradient id="gmv-chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#159c88" /><stop offset="100%" stopColor="#159c88" stopOpacity="0" /></linearGradient></defs><path d="M0 12 L74 12 L148 12 L222 12 L296 12 L370 12 L444 12 L520 12" className="chart-baseline" /><path d="M0 18 L74 24 L148 31 L222 39 L296 45 L370 52 L444 58 L520 64" className="chart-current" /><path d="M0 18 L74 24 L148 31 L222 39 L296 45 L370 52 L444 58 L520 64 L520 72 L0 72Z" className="chart-fill" /></svg></div>
            {reportTask
              ? <button className="feature-footnote feature-footnote-link" onClick={() => onSelect(reportTask.task_id)}>{reportTask.result?.evidence_ids.length ?? 0} 条证据引用 · 点击查看报告</button>
              : <span className="feature-footnote">来自 business.gmv_v1 · 只读指标 · 非实时数据</span>}
          </>;
        })()}</article>
        <article className="summary-stat"><span className="summary-stat-icon"><ListChecks size={16} /></span><span className="summary-stat-label">分析任务</span><strong>{counts.all}</strong><small>当前账号可查看</small></article>
        <article className="summary-stat"><span className="summary-stat-icon summary-icon-amber"><Activity size={16} /></span><span className="summary-stat-label">运行中</span><strong>{counts.active}</strong><small>持久化任务</small></article>
        <article className="summary-stat"><span className="summary-stat-icon summary-icon-green"><CheckCircle2 size={16} /></span><span className="summary-stat-label">已完成</span><strong>{counts.completed}</strong><small>报告可追溯</small></article>
      </section>

      <section className="dashboard-bottom-grid">
        <div className="dashboard-panel recent-panel"><div className="section-heading"><div><span className="kicker">AgentForge Runtime</span><h2>最近分析任务</h2></div><div className="heading-actions"><button className="quiet-button" onClick={onRefresh} aria-label="刷新分析任务"><RefreshCw size={14} />刷新</button><button className="text-button" onClick={() => onNavigate('tasks')}>查看全部<ArrowRight size={14} /></button></div></div>
          {loading && tasks.length === 0 ? <div className="loading-state"><LoaderCircle size={18} className="spin" />正在加载任务…</div> : recentTasks.length === 0 ? <div className="compact-empty"><FileText size={18} /><span>还没有分析任务。先提交一个业务问题，任务和 Trace 会显示在这里。</span><button className="text-button" onClick={onNew}>开始分析<ArrowRight size={14} /></button></div> : <div className="recent-task-list">{recentTasks.map((task) => <button className="recent-task-row" key={task.task_id} onClick={() => onSelect(task.task_id)}><span className="recent-task-icon"><FileCheck2 size={16} /></span><span className="recent-task-main"><strong>{task.task_type === 'business_analysis' ? 'GMV 业务分析' : taskTypeLabel(task.task_type)}</strong><small>{task.task_id.slice(0, 8)} · {relativeDate(task.created_at)}</small></span><StatusPill status={task.status} /><ArrowRight size={15} className="table-arrow" /></button>)}</div>}
        </div>
        <aside className="dashboard-panel capability-panel"><div className="section-heading"><div><span className="kicker">只读数据源</span><h2>业务接入状态</h2></div><Database size={17} className="capability-heading-icon" /></div><div className="source-row source-ready"><span className="source-dot" /><span><strong>GMV v1 指标</strong><small>business.gmv_v1 · 可查询</small></span><span className="source-state">已接入</span></div><div className="source-row source-pending"><span className="source-dot" /><span><strong>库存、退款、流量、营销</strong><small>当前数据契约未提供这些指标</small></span><span className="source-state">待接入</span></div><div className="capability-footnote"><LockKeyhole size={13} />业务查询仅开放只读权限</div></aside>
      </section>
      <div className="overview-footnote"><ShieldCheck size={13} />仅任务所有者可查看分析详情；任务进度通过持久化事件流更新。</div>
    </div>
  );
}

function TaskListPage({ screen, tasks, taskQuestions, loading, onNew, onSelect, onRefresh }: {
  screen: 'tasks' | 'trace' | 'reports';
  tasks: TaskSummary[];
  taskQuestions: Record<string, string>;
  loading: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRefresh: () => void;
}) {
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const isReports = screen === 'reports';
  const visibleTasks = tasks.filter((task) => {
    if (isReports && task.status !== 'completed') return false;
    if (statusFilter !== 'all' && task.status !== statusFilter) return false;
    const label = taskQuestions[task.task_id] ?? task.task_id;
    return `${label} ${task.task_id} ${taskTypeLabel(task.task_type)}`.toLowerCase().includes(query.trim().toLowerCase());
  });
  const title = isReports ? '分析报告' : screen === 'trace' ? '执行 Trace' : '分析任务';
  const description = isReports
    ? '打开已完成的任务，查看结构化指标、证据引用、待验证假设和局限性。'
    : screen === 'trace'
      ? '选择任务查看持久化事件流、执行尝试、恢复与终态。'
      : '查看此账号有权访问的业务分析任务和运行状态。';
  return <div className="task-list-page">
    <section className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" />运营分析</div><h1>{title}</h1><p>{description}</p></div><button className="primary-button" onClick={onNew}><Plus size={16} />新建诊断<ArrowRight size={15} className="button-arrow" /></button></section>
    <section className="task-list-toolbar"><div className="task-list-count"><strong>{visibleTasks.length}</strong><span>{isReports ? '份已完成报告' : '项任务'}</span></div><label className="task-search"><Search size={15} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索任务编号或问题" aria-label="搜索任务" /></label><button className="quiet-button" onClick={onRefresh}><RefreshCw size={14} />刷新</button></section>
    {!isReports && <div className="status-filters" aria-label="按任务状态筛选">{[['all', '全部'], ['running', '运行中'], ['queued', '排队中'], ['completed', '已完成'], ['failed', '失败']].map(([key, label]) => <button key={key} className={statusFilter === key ? 'filter-active' : ''} onClick={() => setStatusFilter(key)}>{label}{key === 'all' ? <span>{tasks.length}</span> : <span>{tasks.filter((task) => task.status === key).length}</span>}</button>)}</div>}
    <section className="task-list-surface">{loading && tasks.length === 0 ? <div className="loading-state"><LoaderCircle size={18} className="spin" />正在加载任务…</div> : visibleTasks.length === 0 ? <div className="empty-state"><div className="empty-icon"><Search size={20} /></div><h3>{query ? '没有匹配的任务' : isReports ? '还没有已完成的报告' : '还没有分析任务'}</h3><p>{query ? '试试任务编号或更短的关键词。' : '创建一项分析任务后，运行状态和报告会保存在工作区中。'}</p>{!query && <button className="primary-button" onClick={onNew}><Plus size={15} />创建分析任务</button>}</div> : <div className="runs-table-wrap"><table className="runs-table"><thead><tr><th>问题 / 任务编号</th><th>类型</th><th>状态</th><th>创建时间</th><th aria-label="打开" /></tr></thead><tbody>{visibleTasks.map((task) => <tr key={task.task_id} onClick={() => onSelect(task.task_id)} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(task.task_id); } }}><td><span className="task-name"><span className="task-row-icon"><FileCheck2 size={16} /></span><span><strong>{taskQuestions[task.task_id] ?? 'GMV 业务分析'}</strong><small>{task.task_id}</small></span></span></td><td>{taskTypeLabel(task.task_type)}</td><td><StatusPill status={task.status} /></td><td className="date-cell">{relativeDate(task.created_at)}</td><td><ArrowRight size={16} className="table-arrow" /></td></tr>)}</tbody></table></div>}</section>
    <div className="overview-footnote"><LockKeyhole size={13} />列表仅包含当前账号有权访问的任务。</div>
  </div>;
}

function ApprovalCenter({ proposals, onUpdate, onOpenTask }: {
  proposals: ApprovalProposal[];
  onUpdate: (id: string, status: Exclude<ApprovalStatus, 'pending'>) => void;
  onOpenTask: (taskId: string) => void;
}) {
  const pendingCount = proposals.filter((proposal) => proposal.status === 'pending').length;
  const statusText: Record<ApprovalStatus, string> = {
    pending: '待审阅', approved: '已批准 · 演示', changes_requested: '已退回修改', rejected: '已拒绝 · 演示',
  };
  return <div className="approval-page">
    <section className="page-intro"><div><div className="eyebrow"><span className="eyebrow-line" />人工把关</div><h1>审批中心</h1><p>检查 Agent 根据分析报告生成的运营建议。</p></div><span className="demo-label approval-demo-label">本地体验流程</span></section>
    <div className="approval-safety-note"><LockKeyhole size={17} /><div><strong>此工作流不会执行业务写操作</strong><p>审批状态只保存在当前浏览器，尚未连接服务端审批或审计 API。库存、营销和售后动作不会被触发。</p></div></div>
    <div className="approval-section-heading"><div><h2>运营行动方案</h2><p>{pendingCount} 项待审阅 · 共 {proposals.length} 项</p></div></div>
    {proposals.length === 0 ? <div className="empty-state approval-empty"><div className="empty-icon"><ShieldCheck size={21} /></div><h3>还没有待审阅方案</h3><p>完成一项分析后，在报告中点击“生成审批草案”，把建议带到这里。</p></div> : <div className="approval-list">{proposals.map((proposal) => <article className="approval-card" key={proposal.id}>
      <div className="approval-card-header"><div><span className="approval-id">方案 {proposal.id.slice(0, 8)} · 关联任务 {proposal.taskId.slice(0, 8)}</span><h3>{proposal.title}</h3></div><span className={`approval-status status-${proposal.status}`}>{statusText[proposal.status]}</span></div>
      <p className="approval-summary">{proposal.summary}</p>
      <button className="text-button approval-source-link" onClick={() => onOpenTask(proposal.taskId)}><FileCheck2 size={14} />查看原始分析报告<ArrowUpRight size={13} /></button>
      <div className="proposal-evidence">{proposal.evidenceIds.map((id) => <span className="evidence-chip" key={id}><FileCheck2 size={12} />{id}</span>)}</div>
      <ol className="proposal-recommendations">{proposal.recommendations.map((recommendation, index) => <li key={`${proposal.id}-${index}`}><span>{String(index + 1).padStart(2, '0')}</span><p>{recommendation}</p></li>)}</ol>
      <div className="approval-audit-preview"><span><CalendarDays size={13} />创建于 {relativeDate(proposal.createdAt)}</span>{proposal.history.slice(-2).map((entry, index) => <span key={`${entry.at}-${index}`}>{entry.actor} · {entry.action} · {relativeDate(entry.at)}</span>)}</div>
      {proposal.status === 'pending' && <div className="approval-actions"><span>审阅操作只改变本地演示状态</span><div><button className="quiet-button" onClick={() => onUpdate(proposal.id, 'changes_requested')}>退回修改</button><button className="danger-quiet-button" onClick={() => onUpdate(proposal.id, 'rejected')}><Ban size={14} />拒绝</button><button className="primary-button" onClick={() => onUpdate(proposal.id, 'approved')}><Check size={15} />批准方案</button></div></div>}
    </article>)}</div>}
  </div>;
}

function CreateTask({ token, user, initialQuestion, onQuestionChange, onCreated, onProblem, onCancel }: {
  token: string;
  user: Principal;
  initialQuestion: string;
  onQuestionChange: (value: string) => void;
  onCreated: (id: string, question: string) => void;
  onProblem: (problem: ApiProblem | null) => void;
  onCancel: () => void;
}) {
  const [agentKey, setAgentKey] = useState('sales-report');
  const defaultRegion = user.regions.find((grant) => grant.region_code === 'CN_EAST')?.region_code ?? user.regions[0]?.region_code ?? '';
  const [region, setRegion] = useState(defaultRegion);
  const [start, setStart] = useState(SAMPLE_START);
  const [end, setEnd] = useState(SAMPLE_END_EXCLUSIVE);
  const [timezone, setTimezone] = useState('UTC');
  const [compareMode, setCompareMode] = useState('previous_period');
  const [busy, setBusy] = useState(false);
  const rangeDays = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000;
  const rangeValid = start < end && rangeDays > 0 && rangeDays <= 90;

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
            question: initialQuestion.trim(),
            region_codes: [region],
            date_range: { start, end_exclusive: end, timezone },
            compare_mode: compareMode,
          },
        }),
      });
      onCreated(response.task_id, initialQuestion.trim());
    } catch (error) {
      onProblem(problemFrom(error));
      setBusy(false);
    }
  }

  return (
    <div className="create-page"><button className="back-link" onClick={onCancel}><ArrowLeft size={15} />返回运营工作台</button><section className="page-intro create-intro"><div><div className="eyebrow"><span className="eyebrow-line" />新建运营诊断</div><h1>把问题交给已发布的 Agent</h1><p>任务只使用当前账号已授权的区域和平台已接入的只读业务指标。</p></div></section>
      <div className="create-layout"><form className="create-card" onSubmit={submit}><div className="form-section-heading"><span className="step-number">01</span><div><h2>分析问题</h2><p>明确希望核验的业务变化和输出边界。</p></div></div>
        <div className="field-grid single-field-grid"><div className="field-group"><label htmlFor="analysis-question">运营问题</label><textarea id="analysis-question" value={initialQuestion} onChange={(event) => onQuestionChange(event.target.value)} rows={4} maxLength={10000} required /><small>{initialQuestion.length} / 10,000 · 未验证的原因应保留为假设。</small></div></div>
        <div className="form-divider" /><div className="form-section-heading"><span className="step-number">02</span><div><h2>分析范围</h2><p>选择已发布的 Agent 和你有权访问的业务区域。</p></div></div>
        <div className="field-grid"><div className="field-group"><label htmlFor="agent">Agent 标识</label><input id="agent" value={agentKey} onChange={(event) => setAgentKey(event.target.value)} required maxLength={128} placeholder="sales-report" /><small>此分析将使用已发布的 Agent 版本。</small></div><div className="field-group"><label htmlFor="region">业务区域</label><select id="region" value={region} onChange={(event) => setRegion(event.target.value)} required>{user.regions.length === 0 && <option value="">暂无可用区域</option>}{user.regions.map((grant) => <option key={grant.region_code} value={grant.region_code}>{grant.region_code} · {grant.permission === 'read' ? '只读' : '管理'}权限</option>)}</select><small>这里只显示已授权给你的区域。</small></div></div>
        <div className="form-divider" /><div className="form-section-heading"><span className="step-number">03</span><div><h2>时间范围</h2><p>开始日期包含，结束日期不包含；默认落在 GMV 下滑标杆窗口。</p></div></div>
        <div className="field-grid date-grid"><div className="field-group"><label htmlFor="start-date">开始日期 <span className="label-note">包含</span></label><input id="start-date" type="date" value={start} min="2025-01-01" max={end} onChange={(event) => setStart(event.target.value)} required /></div><div className="date-separator"><ArrowRight size={16} /></div><div className="field-group"><label htmlFor="end-date">结束日期 <span className="label-note">不包含</span></label><input id="end-date" type="date" value={end} min={start} max="2025-06-30" onChange={(event) => setEnd(event.target.value)} required /></div></div>
        <div className="field-grid lower-grid"><div className="field-group"><label htmlFor="timezone">时区</label><input id="timezone" value={timezone} onChange={(event) => setTimezone(event.target.value)} required placeholder="UTC" /></div><div className="field-group"><label htmlFor="compare">对比方式</label><select id="compare" value={compareMode} onChange={(event) => setCompareMode(event.target.value)}><option value="previous_period">上一周期</option></select></div></div>
        <div className="form-summary"><span className="summary-icon"><Sparkles size={17} /></span><span><strong>执行范围预览</strong><small>{region || '请选择区域'} · {start} 至 {end}（结束日期不含）· 对比前一周期</small></span></div>
        {!rangeValid && <p className="range-error" role="alert">请检查日期范围。结束日期要晚于开始日期，且最多 90 天。</p>}
        <div className="form-actions"><span><LockKeyhole size={13} />仅调用已发布 Agent 的只读业务工具</span><button type="submit" className="primary-button" disabled={busy || !region || !initialQuestion.trim() || !rangeValid}>{busy ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}{busy ? '正在创建…' : '创建分析任务'}<ArrowRight size={15} className="button-arrow" /></button></div>
      </form><aside className="create-aside"><div className="aside-icon"><ShieldCheck size={19} /></div><h3>先验证，再给建议</h3><p>当前执行图使用单个已发布 Agent。报告会显示 GMV 数据证据，并将没有数据支撑的解释保留为假设。</p><div className="aside-divider" /><div className="aside-feature"><span><Radio size={15} /></span><div><strong>实时事件流</strong><small>离开页面后，任务仍由持久化运行时处理。</small></div></div><div className="aside-feature"><span><FileCheck2 size={15} /></span><div><strong>报告带证据</strong><small>指标、发现、假设和局限性来自任务结果。</small></div></div><div className="aside-note"><TriangleAlert size={14} />当前合成业务快照覆盖 2025-01-01 至 2025-06-30；库存、退款和流量表尚未接入。</div></aside></div>
    </div>
  );
}

function TaskPage({ taskId, question, detail, events, streamState, loading, onBack, onRefresh, onCancel, onCreateApproval }: {
  taskId: string;
  question?: string;
  detail: TaskDetail | null;
  events: TaskEvent[];
  streamState: 'connecting' | 'live' | 'reconnecting';
  loading: boolean;
  onBack: () => void;
  onRefresh: () => void;
  onCancel: (task: TaskDetail) => Promise<void>;
  onCreateApproval: (taskId: string, report: Report) => void;
}) {
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const completedSteps = Math.max(detail?.progress.completed_steps ?? 0, events.filter((event) => event.type === 'step.completed').length);
  const totalSteps = detail?.progress.total_steps ?? 4;

  async function cancelCurrentTask() {
    if (!detail) return;
    setCancelling(true);
    try {
      await onCancel(detail);
      setConfirmCancel(false);
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="task-page"><button className="back-link" onClick={onBack}><ArrowLeft size={15} />返回任务列表</button>{loading && !detail ? <div className="loading-state task-loading"><LoaderCircle size={20} className="spin" />正在加载任务详情…</div> : detail ? <>
      <section className="task-heading"><div><div className="eyebrow"><span className="eyebrow-line" />{screenLabelForTask(detail.task_type)} <span className="heading-dot">·</span> {taskId.slice(0, 8)}</div><h1>{question || 'GMV 业务分析'}</h1><p>提交时间：{relativeDate(detail.created_at)}{detail.current_attempt_no ? ` · 第 ${detail.current_attempt_no} 次尝试` : ''}</p></div><div className="task-heading-actions"><StatusPill status={detail.status} />{!TERMINAL.has(detail.status) && detail.status !== 'cancelling' && <button className="danger-quiet-button" onClick={() => setConfirmCancel((value) => !value)}><Ban size={14} />取消任务</button>}<button className="quiet-button" onClick={onRefresh} aria-label="刷新任务"><RefreshCw size={15} />刷新</button></div></section>
      {confirmCancel && <div className="cancel-confirmation" role="status"><span><CircleAlert size={16} />确认取消这项分析？运行中的步骤会收到取消请求。</span><div><button className="quiet-button" onClick={() => setConfirmCancel(false)} disabled={cancelling}>继续运行</button><button className="danger-quiet-button" onClick={() => void cancelCurrentTask()} disabled={cancelling}>{cancelling ? <LoaderCircle size={14} className="spin" /> : <Ban size={14} />}{cancelling ? '正在提交…' : '确认取消'}</button></div></div>}
      <div className="task-columns"><section className="task-primary"><div className="panel report-panel"><div className="panel-header"><div><span className="kicker">可追溯分析</span><h2>{TERMINAL.has(detail.status) ? detail.status === 'completed' ? '分析结果已生成' : '任务摘要' : '分析正在运行'}</h2></div><div className="panel-heading-actions">{detail.result && <span className="version-tag">报告 v{detail.result.report_version}</span>}{detail.result && <button className="quiet-button" onClick={() => window.print()}><Download size={14} />导出 / 打印</button>}{detail.result && <button className="primary-button compact-primary" onClick={() => onCreateApproval(taskId, detail.result!)}><ShieldCheck size={14} />生成审批草案</button>}</div></div>
          {question && <div className="question-context"><span>提交的问题</span><p>{question}</p></div>}
          {detail.status === 'completed' && detail.result ? <ReportView report={detail.result} /> : detail.status === 'failed' ? <div className="task-error"><span className="task-error-icon"><TriangleAlert size={18} /></span><div><strong>{detail.error?.code ?? 'TASK_FAILED'}</strong><p>{detail.error ? localizeError(detail.error.code) : '分析未能完成，请查看任务动态了解详情。'}</p><button className="text-button" onClick={onRefresh}>刷新任务状态<ArrowRight size={13} /></button></div></div> : detail.status === 'cancelled' ? <div className="progress-state"><div className="progress-illustration progress-cancelled"><X size={22} /></div><h3>分析任务已取消</h3><p>任务结束时未生成分析报告。</p></div> : <div className="progress-state"><div className="progress-illustration"><LoaderCircle size={22} className="spin" /></div><h3>{detail.status === 'queued' ? '分析任务正在排队' : detail.status === 'retry_wait' || detail.status === 'recovering' ? '运行时正在恢复分析' : detail.status === 'cancelling' ? '正在安全停止任务' : '分析正在进行'}</h3><p>任务状态由服务端运行时维护。此页面可关闭，持久化事件会在重新打开后续播。</p><div className="progress-steps">{['制定计划', '查询 GMV', '整理结果', '检查报告'].map((step, index) => <span key={step} className={index < completedSteps ? 'step-done' : index === completedSteps && detail.status === 'running' ? 'step-current' : ''}><span>{index < completedSteps ? <Check size={12} /> : index + 1}</span>{step}</span>)}</div></div>}
        </div>
        {detail.result && <ReportEvidenceFooter report={detail.result} />}
        {detail.result && <div className="report-scope-note"><ShieldCheck size={14} />本报告来自已授权区域与冻结数据快照；当前结果只覆盖 GMV v1，其他业务因素需接入数据后再验证。</div>}
      </section>
      <aside className="task-aside"><div className="panel live-panel"><div className="panel-header"><div><span className="kicker">Durable Runtime</span><h2>执行 Trace</h2></div><span className={`stream-indicator stream-${streamState}`}><span className="status-dot" />{streamState === 'live' ? '实时连接' : streamState === 'connecting' ? '正在连接' : '正在重连'}</span></div><div className="trace-summary"><span><strong>{events.length}</strong><small>已接收事件</small></span><span><strong>{events.filter((event) => event.type === 'task.attempt.started').length || detail.current_attempt_no || 0}</strong><small>执行尝试</small></span><span><strong>{completedSteps}/{totalSteps}</strong><small>已完成步骤</small></span></div><div className="event-list">{events.length === 0 && <div className="event-empty"><LoaderCircle size={16} className="spin" />正在等待任务事件…</div>}{[...events].reverse().map((event) => <article className="event-item" key={event.sequence}><span className="event-marker"><span /></span><div><div className="event-title"><strong>{eventLabel(event.type)}</strong><span>#{event.sequence}</span></div>{eventDetails(event) && <p>{eventDetails(event)}</p>}<time>接收于 {relativeDate(event.receivedAt)}</time></div></article>)}</div><div className="event-panel-footer"><span><ShieldCheck size={13} />重连时使用 Last-Event-ID 续播</span><span>按序号去重</span></div></div>
        <div className="panel task-info"><span className="kicker">任务信息</span><dl><div><dt>任务 ID</dt><dd className="mono-value">{detail.task_id}</dd></div><div><dt>类型</dt><dd>{taskTypeLabel(detail.task_type)}</dd></div><div><dt>业务区域</dt><dd>{detail.input?.region_codes.join('、') || '受授权区域'}</dd></div><div><dt>时间范围</dt><dd>{detail.input ? `${detail.input.date_range.start} 至 ${detail.input.date_range.end_exclusive} · ${detail.input.date_range.timezone}` : '见分析问题'}</dd></div><div><dt>创建时间</dt><dd>{relativeDate(detail.created_at)}</dd></div><div><dt>最近更新</dt><dd>{relativeDate(detail.updated_at)}</dd></div><div><dt>当前尝试</dt><dd>{detail.current_attempt_no ?? '尚未开始'}</dd></div><div><dt>乐观版本</dt><dd>{detail.version}</dd></div></dl></div>
      </aside></div>
      </> : <div className="empty-state"><div className="empty-icon"><CircleAlert size={22} /></div><h3>无法加载任务详情</h3><p>刷新页面以获取任务的最新状态。</p><button className="primary-button" onClick={onRefresh}><RefreshCw size={15} />重试</button></div>}</div>
  );
}

function screenLabelForTask(type: string): string {
  return type === 'business_analysis' ? 'GMV 业务分析' : '分析任务';
}

function money(value: string, unit: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `${value} ${unit}`;
  const formatted = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(parsed);
  return ['currency', 'cny'].includes(unit.toLowerCase()) ? `¥${formatted}` : `${formatted} ${unit}`;
}

function ReportView({ report }: { report: Report }) {
  return <div className="report-content"><p className="report-summary">{report.summary}</p><section className="report-section"><div className="report-section-title"><span className="report-section-icon"><Activity size={15} /></span><div><h3>核验指标</h3><p>根据任务获授权的数据范围计算。</p></div></div>{report.metrics.length ? <div className="metric-table-wrap"><table className="metric-table"><thead><tr><th>指标</th><th>当前值</th><th>上一周期</th><th>变化</th></tr></thead><tbody>{report.metrics.map((metric) => <tr key={metric.evidence_id}><td><strong>{metric.metric === 'gmv' ? '成交总额' : metric.metric}</strong><small>版本 {metric.metric_version}</small></td><td>{money(metric.current_value, metric.unit)}</td><td>{money(metric.previous_value, metric.unit)}</td><td className={metric.change_rate && metric.change_rate.startsWith('-') ? 'negative-change' : 'positive-change'}>{metric.change_rate === null ? <span className="null-change">暂无数据</span> : `${(Number(metric.change_rate) * 100).toFixed(2)}%`}</td></tr>)}</tbody></table></div> : <p className="report-empty">报告中没有包含指标数据。</p>}</section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon"><ArrowUpRight size={15} /></span><div><h3>分析发现</h3><p>由已核验证据支持的观察结果。</p></div></div><div className="report-list">{report.findings.map((finding, index) => <article className="report-list-item finding-item" key={`${finding.evidence_ids.join('-')}-${index}`}><span className="list-index">{String(index + 1).padStart(2, '0')}</span><div><p>{finding.text}</p><div className="evidence-chips">{finding.evidence_ids.map((id) => <span className="evidence-chip" key={id}><FileCheck2 size={12} />{id}</span>)}</div></div></article>)}</div></section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon hypothesis-icon"><Sparkles size={15} /></span><div><h3>待验证假设</h3><p>仍需进一步核实的可能解释。</p></div></div>{report.hypotheses.length ? <div className="report-list">{report.hypotheses.map((hypothesis, index) => <article className="report-list-item hypothesis-item" key={index}><span className="hypothesis-badge">尚未验证</span><p>{hypothesis.text}</p></article>)}</div> : <p className="report-empty">报告中没有待验证假设。</p>}</section>
    <section className="report-section"><div className="report-section-title"><span className="report-section-icon recommendation-icon"><CheckCircle2 size={15} /></span><div><h3>建议</h3><p>根据本次分析提出的后续步骤。</p></div></div><ul className="recommendation-list">{report.recommendations.map((item, index) => <li key={index}><span><ArrowRight size={13} /></span>{item}</li>)}</ul></section>
    <section className="report-section limitations-section"><div className="report-section-title"><span className="report-section-icon limitation-icon"><TriangleAlert size={15} /></span><div><h3>局限性</h3><p>使用本报告时需要考虑的背景信息。</p></div></div>{report.limitations.length ? <ul className="limitation-list">{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="report-empty">报告未列出局限性。</p>}</section>
  </div>;
}

function ReportEvidenceFooter({ report }: { report: Report }) {
  const snapshotId = report.metrics.find((metric) => metric.data_snapshot_id)?.data_snapshot_id;
  return <div className="report-footnote"><span><FileText size={14} />{report.evidence_ids.length} 条证据引用</span><span title={snapshotId ?? undefined}>数据快照 {snapshotId ? snapshotId.slice(0, 12) : '未返回'} · v{report.report_version}</span></div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
