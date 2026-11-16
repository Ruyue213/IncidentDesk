import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { api, ApiError } from './api.ts';
import { approvalPayload, isTerminal, preferRun, readStore, writeStore } from './state.ts';
import type { BusinessEvent, EvidenceSnapshot, Incident, ModelSettings, ProjectProfile, Readiness, Reference, Run, RunStatus } from './types.ts';
import { useRun } from './useRun.ts';
import Icon from './Icon.tsx';
import NewRunDialog from './NewRunDialog.tsx';
import SettingsDialog from './SettingsDialog.tsx';
import ServiceActionPanel from './ServiceActionPanel.tsx';
import s from './App.module.css';

const STATUS: Record<RunStatus, string> = {
  QUEUED: '已排队', RUNNING: '执行中', WAITING_APPROVAL: '待人工批准',
  SUCCEEDED: '已完成', FAILED: '执行失败', CANCELLED: '已取消',
};
const connectionLabel = { idle: '未选择任务', connecting: '连接中', live: '实时同步', reconnecting: '正在重连 · 轮询恢复', complete: '终态已同步' };
const time = (iso: string, full = false) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  ...(full ? { second: '2-digit' } : {}), hourCycle: 'h23',
}).format(new Date(iso));
const fee = (amount: number) => `¥${amount.toFixed(amount > 0 && amount < 0.01 ? 6 : 4)}`;
const shortId = (id: string) => id.slice(0, 8);
const shortVersion = (version: string) => {
  if (version.length <= 24) return version;
  const prefix = version.startsWith('sha256:') ? 'sha256:' : '';
  return `${prefix}${version.slice(prefix.length, prefix.length + 8)}…${version.slice(-6)}`;
};

function Badge({ status }: { status: RunStatus }) { return <span className={s.badge} data-state={status}><span />{STATUS[status]}</span>; }
function refsFor(run: Run | null): Reference[] {
  const unique = new Map<string, Reference>();
  for (const reference of [
    ...(run?.diagnosis?.supporting_evidence || []), ...(run?.diagnosis?.contradicting_evidence || []),
    ...(run?.operation?.params.references || []), ...(run?.operation?.params.request?.references || []),
  ]) unique.set(`${reference.id}:${reference.version}`, reference);
  return [...unique.values()];
}
function errorMessage(run: Run) {
  if (!run.error) return '任务已停止，暂未生成有效诊断。';
  if (typeof run.error === 'string') return run.error;
  const labels: Record<string, string> = {
    BudgetExceeded: '调用、费用或活跃执行时间已达到上限，任务已停止。',
    TimeoutError: '执行或模型请求超时，任务已停止。',
    AuthenticationError: '模型服务认证失败，请检查后端配置。',
    APIConnectionError: '暂时无法连接模型服务，本次未执行隐藏重试。',
    RateLimitError: '模型服务限流，请稍后创建新任务。',
    ValueError: '报告、引用或工具参数未通过校验。',
    ValidationError: '模型返回的报告或工具参数未通过格式校验，任务已停止。你可以查看错误类型，或新建任务重试。',
    StructuredOutputValidationError: '模型返回的结构化报告未通过校验，未将它当作有效诊断。请核对错误详情，或新建任务重试。',
    InvalidModelOutput: '模型未返回可用的诊断格式，任务已停止，没有据此推测结论或执行后续操作。',
    Conflict: '冻结草案、批准或来源快照发生冲突，执行已停止。',
  };
  return labels[run.error.type || ''] || run.error.message || run.error.code || '执行已停止，请核对本地记录。';
}
function narrative(run: Run) {
  if (run.cancel_requested && !isTerminal(run.status)) return '取消意图已记录，后台将在下一个执行边界停止；已执行动作和回执会保留。';
  switch (run.status) {
    case 'QUEUED': return '任务已持久提交，等待后台执行。';
    case 'RUNNING': return '后台正在处理诊断，关闭页面后任务仍可继续。';
    case 'WAITING_APPROVAL': return run.operation?.decision ? '人工决定已记录，等待后台继续处理。' : run.operation?.kind === 'service_action' ? '服务操作与引用已冻结，请核对目标、命令或配置差异后做出决定。' : '工单草案与引用已冻结，请核对来源后做出决定。';
    case 'SUCCEEDED': return run.action_receipt ? '任务已完成；服务动作的实际执行与恢复检查结果见下方回执。' : run.ticket_id ? '诊断已完成，工单写入回执已确认。' : run.operation?.decision === 'reject' ? '诊断已完成；本次操作申请已拒绝。' : '诊断已完成，可继续核对引用与下一步检查。';
    case 'CANCELLED': return '任务已取消；已发生的动作与回执仍保留，请核对实际状态。';
    case 'FAILED': return errorMessage(run);
  }
}
function eventLabel(event: BusinessEvent): string {
  switch (event.kind) {
    case 'run.queued': return '任务已提交';
    case 'run.state_changed': return `任务状态 · ${STATUS[event.payload.status as RunStatus] || String(event.payload.status)}`;
    case 'run.cancel_requested': return '取消意图已记录';
    case 'operation.frozen': return '操作预览已冻结，等待人工决定';
    case 'operation.decided': return event.payload.decision === 'approve' ? '人工批准已记录' : '人工拒绝已记录';
    case 'ticket.created': return '本地工单已创建，回执已保存';
    case 'action.started': return '已批准的服务动作开始执行';
    case 'action.finished': return '服务动作执行回执已保存';
    case 'tool.started': return `开始采集 / 调用工具 · ${String(event.payload.tool || '未标注')}`;
    case 'tool.finished': return `${event.payload.status === 'error' ? '采集 / 工具返回错误' : '采集 / 工具调用结束'} · ${String(event.payload.tool || '未标注')}`;
    case 'report.validation_failed': return `引用校验反馈，第 ${String(event.payload.attempt)} 次修正`;
    default: return event.kind;
  }
}

export default function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => readStore<string>(localStorage, 'incidentdesk.selectedRun'));
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [projects, setProjects] = useState<ProjectProfile[]>([]);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [settingsError, setSettingsError] = useState('');
  const [settingsTab, setSettingsTab] = useState<'projects' | 'model'>('projects');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [followupParent, setFollowupParent] = useState<Run | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(50);
  const [filter, setFilter] = useState('all');
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const actionRef = useRef(false);
  const [activeReference, setActiveReference] = useState<(Reference & { ownerRunId: string }) | null>(null);
  const [snapshot, setSnapshot] = useState<{ ownerRunId: string; item: EvidenceSnapshot } | null>(null);
  const [snapshotError, setSnapshotError] = useState('');
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 1190px)').matches);
  const evidencePanel = useRef<HTMLElement>(null);
  const referenceTrigger = useRef<HTMLButtonElement | null>(null);
  const updateRun = useCallback((run: Run) => {
    setRuns((previous) => {
      const map = new Map(previous.map((item) => [item.id, item]));
      map.set(run.id, preferRun(map.get(run.id), run));
      return [...map.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
    });
  }, []);
  const detail = useRun(selectedId, updateRun);
  // A newly selected ID must never render or act on the previous run's detail.
  const run = detail.run?.id === selectedId ? detail.run : null;
  const loadSettings = useCallback(async () => {
    const results = await Promise.allSettled([api.projects(), api.modelSettings()]);
    if (results[0].status === 'fulfilled') setProjects(results[0].value.items);
    if (results[1].status === 'fulfilled') setModelSettings(results[1].value);
    setSettingsError(results.some((result) => result.status === 'rejected') ? '项目或模型设置未能读取，请确认本机服务可用后重试。' : '');
  }, []);
  const loadReadiness = useCallback(async () => {
    try { setReadiness(await api.readiness()); } catch { setReadiness(null); }
  }, []);
  const openSettings = (tab: 'projects' | 'model') => { setSettingsTab(tab); setSettingsOpen(true); void loadSettings(); };
  const openNew = () => { setFollowupParent(null); setNewOpen(true); };

  const loadRuns = useCallback(async () => {
    try {
      const page = await api.runs();
      setRuns((previous) => {
        const map = new Map(previous.map((item) => [item.id, item]));
        for (const item of page.items) map.set(item.id, preferRun(map.get(item.id), item));
        return [...map.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
      });
      setHasMore(page.items.length === 50);
      setApiOnline(true);
    } catch { setApiOnline(false); }
    finally { setListLoading(false); }
  }, []);
  useEffect(() => {
    void loadRuns();
    void loadSettings(); void loadReadiness();
    void api.incidents().then((page) => setIncidents(page.items)).catch(() => setToast({ message: '无法读取模拟案例，请确认本机服务可用后刷新。', error: true }));
    const poll = window.setInterval(() => { void loadRuns(); void loadReadiness(); }, 5000);
    return () => window.clearInterval(poll);
  }, [loadRuns, loadSettings, loadReadiness]);
  useEffect(() => {
    if (!selectedId && runs[0]) setSelectedId(runs[0].id);
  }, [runs, selectedId]);
  useEffect(() => {
    if (selectedId) writeStore(localStorage, 'incidentdesk.selectedRun', selectedId);
    setActiveReference(null); setSnapshot(null); setSnapshotError('');
  }, [selectedId]);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1190px)');
    const changed = () => setCompact(query.matches);
    query.addEventListener('change', changed);
    return () => query.removeEventListener('change', changed);
  }, []);
  useEffect(() => {
    if (!activeReference || activeReference.ownerRunId !== selectedId || run?.id !== selectedId) return;
    const ownerRunId = activeReference.ownerRunId;
    const abort = new AbortController();
    setSnapshot(null); setSnapshotError('');
    void api.evidence(ownerRunId, activeReference.id, abort.signal).then((item) => {
      if (abort.signal.aborted) return;
      if (item.id !== activeReference.id || String(item.version) !== activeReference.version) {
        setSnapshotError('引用与本次快照的版本不一致，请刷新任务后重新核对。'); return;
      }
      setSnapshot({ ownerRunId, item });
    }).catch((failure: unknown) => {
      if (!abort.signal.aborted) setSnapshotError(failure instanceof Error ? failure.message : '来源暂时无法读取');
    });
    if (compact) evidencePanel.current?.focus();
    return () => abort.abort();
  }, [activeReference, selectedId, compact, run?.id]);

  const references = useMemo(() => refsFor(run), [run]);
  const visibleSnapshot = snapshot && activeReference
    && snapshot.ownerRunId === selectedId && activeReference.ownerRunId === selectedId
    && run?.id === selectedId && snapshot.item.id === activeReference.id
    && String(snapshot.item.version) === activeReference.version ? snapshot.item : null;
  const snapshotTime = visibleSnapshot?.observed_at || visibleSnapshot?.timestamp;
  const filteredRuns = runs.filter((item) => filter === 'all'
    || (filter === 'approval' && item.status === 'WAITING_APPROVAL')
    || (filter === 'active' && ['QUEUED', 'RUNNING'].includes(item.status))
    || (filter === 'done' && isTerminal(item.status)));
  const selectRun = (id: string) => {
    selectedRef.current = id; setSelectedId(id); setToast(null);
    setActiveReference(null); setSnapshot(null); setSnapshotError('');
  };
  const openEvidence = (reference: Reference, trigger: HTMLButtonElement) => {
    if (!run || run.id !== selectedId || run.id !== selectedRef.current) return;
    referenceTrigger.current = trigger;
    setSnapshot(null); setSnapshotError('');
    setActiveReference({ ...reference, ownerRunId: run.id });
  };
  const closeEvidence = () => { setActiveReference(null); referenceTrigger.current?.focus(); };
  const evidenceKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') { closeEvidence(); return; }
    if (!compact || event.key !== 'Tab') return;
    const panel = evidencePanel.current;
    if (!panel) return;
    const controls = [...panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((element) => element.getClientRects().length > 0);
    const first = controls[0]; const last = controls.at(-1);
    if (!first || !last) { event.preventDefault(); panel.focus(); return; }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
    // Preserve native Tab order between the close button and source references.
  };
  const referenceButton = (reference: Reference, index = 0) => <button
    key={`${reference.id}:${index}`} className={s.referenceChip} type="button"
    aria-label={`查看证据 ${reference.id}`} onClick={(event) => openEvidence(reference, event.currentTarget)}
    data-selected={activeReference?.ownerRunId === run?.id && activeReference?.id === reference.id}
  ><Icon name="evidence" size={13} /><span>{reference.id}</span><Icon name="chevron" size={12} /></button>;

  async function perform(action: 'approve' | 'reject' | 'cancel') {
    if (!run || actionRef.current) return;
    const targetId = run.id;
    const body = run.operation && action !== 'cancel' ? approvalPayload(run.operation, action) : null;
    actionRef.current = true; setBusy(action); setToast(null);
    try {
      if (action === 'cancel') await api.cancel(targetId);
      else if (body) await api.decide(targetId, body);
      else return;
      if (selectedRef.current === targetId) setToast({ message: action === 'approve' ? '批准已记录。实际执行结果以下方服务端回执为准。' : action === 'reject' ? '拒绝已记录。后台将保留诊断结果，不执行本次申请的操作。' : '取消意图已记录，等待后台在执行边界处理。', error: false });
    } catch (failure) {
      if (selectedRef.current === targetId) setToast({ message: failure instanceof ApiError && failure.status === 409
        ? '草案或决定已变化，操作未执行。已刷新当前任务，请重新核对。'
        : failure instanceof Error ? failure.message : '操作结果尚未确认，请刷新任务核对。', error: true });
    } finally {
      await Promise.allSettled([detail.refresh(), loadRuns()]);
      actionRef.current = false; setBusy(null);
    }
  }
  async function more() {
    try {
      const page = await api.runs(50, offset);
      for (const item of page.items) updateRun(item);
      setOffset(offset + 50); setHasMore(page.items.length === 50);
    } catch { setToast({ message: '无法读取更多任务，请稍后重试。', error: true }); }
  }
  const pendingDecision = run?.status === 'WAITING_APPROVAL' && run.operation?.status === 'PENDING' && !run.operation.decision && !run.cancel_requested;
  const serviceAction = run?.operation?.kind === 'service_action';
  const draft = serviceAction ? null : run?.operation?.params.draft || run?.diagnosis?.ticket_draft;
  const operationLabel = run?.ticket_id ? '已创建工单' : run?.operation?.decision === 'reject' ? '已拒绝建单' : run?.operation?.decision === 'approve' ? '已批准 · 待执行' : run?.operation ? '等待人工批准' : '仅诊断草案';

  return <div className={s.app}>
    <header className={s.topbar}>
      <a className={s.brand} href="/" aria-label="IncidentDesk 工作台"><span className={s.brandMark}><i /><i /><i /></span><span>Incident<span className={s.brandLight}>Desk</span></span><span className={s.brandDivider} /><span className={s.productName}>诊断与证据工作台</span></a>
      <div className={s.headerMeta}><span className={s.localBadge}>本机 · 单用户</span><span className={s.apiStatus} data-online={apiOnline}><span />{apiOnline === null ? '连接服务中' : apiOnline ? 'API 已连接' : 'API 连接中断'}</span><button type="button" className={s.headerButton} onClick={() => openSettings('projects')}>项目设置</button><button type="button" className={s.headerButton} onClick={() => openSettings('model')}>模型设置</button></div>
    </header>
    <main className={s.workspace}>
      <aside className={s.sidebar} aria-label="诊断任务列表">
        <div className={s.sidebarHeader}><div><span className={s.eyebrow}>WORKSPACE</span><h1>诊断任务<span className={s.count}>{runs.length}</span></h1></div><button className={s.iconButton} type="button" aria-label="刷新任务列表" title="刷新任务列表" onClick={() => { void loadRuns(); void detail.refresh(); }}><Icon name="refresh" /></button></div>
        <button className={s.primaryButton} type="button" aria-label="新建诊断任务" onClick={openNew}><Icon name="plus" size={18} />新建诊断任务</button>
        <div className={s.readinessSummary} aria-label="本机就绪状态"><span data-ready={readiness?.worker.status === 'ready'}>后台执行：{!readiness ? '状态未确认' : readiness.worker.status === 'ready' ? `在线 · ${readiness.worker.mode === 'offline' ? '离线演示模式' : readiness.worker.mode === 'live' ? '云端模型模式' : '执行模式未确认'}` : readiness.worker.status === 'stale' ? '心跳过期' : '不可用'}</span><button type="button" onClick={() => openSettings('model')}>模型：{!readiness ? '状态未确认' : readiness.model.status === 'configured' ? '已配置 · 连接待验证' : '未配置密钥'}</button><button type="button" onClick={() => openSettings('projects')}>采样：{({ ready: '运行正常', degraded: '部分失败', error: '采集失败', idle: '尚无采集来源', stale: '状态过期', unavailable: '状态未确认' })[readiness?.sampler?.status || 'unavailable']} · {projects.length} 个项目</button></div>
        <div className={s.filterBar} role="group" aria-label="任务状态筛选">{[['all', '全部'], ['approval', '待批准'], ['active', '进行中'], ['done', '已结束']].map(([value, label]) => <button key={value} type="button" data-active={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</div>
        <div className={s.taskList}>
          {listLoading && <p className={s.listEmpty}>正在读取持久任务…</p>}
          {!listLoading && !filteredRuns.length && <p className={s.listEmpty}>{filter === 'all' ? '还没有诊断任务。\n从一个具体问题开始。' : '这个状态下暂无任务。'}</p>}
          {filteredRuns.map((item) => <button className={s.taskItem} key={item.id} data-active={selectedId === item.id} type="button" aria-label={`选择任务 ${shortId(item.id)} ${STATUS[item.status]}`} aria-pressed={selectedId === item.id} onClick={() => selectRun(item.id)}>
            <span className={s.taskMeta}><span className={s.mono}>{item.request.mode === 'live' ? projects.find((project) => project.id === item.request.service_id)?.name || item.request.service_id : `Demo · ${item.request.incident_id}`}</span><span>{time(item.created_at)}</span></span>
            <strong>{item.request.question}</strong><span className={s.taskFooter}><Badge status={item.status} /><span className={s.mono}>#{shortId(item.id)}</span></span>
          </button>)}
          {hasMore && <button type="button" className={s.loadMore} onClick={() => { void more(); }}>加载更多任务</button>}
        </div>
        <div className={s.sidebarNote}><Icon name="info" size={15} /><span>状态以服务端为准。刷新页面或关闭连接不会取消后台任务。</span></div>
      </aside>

      <section className={s.mainColumn} aria-label="任务诊断详情">
        {toast && <div className={toast.error ? s.errorToast : s.successToast} role={toast.error ? 'alert' : 'status'}><Icon name={toast.error ? 'info' : 'check'} size={17} /><span>{toast.message}</span><button type="button" aria-label="关闭提示" onClick={() => setToast(null)}><Icon name="close" size={16} /></button></div>}
        {!run ? <div className={s.welcome}><span className={s.emptyIcon}><Icon name="evidence" size={32} /></span><span className={s.eyebrow}>EVIDENCE FIRST</span><h2>{selectedId ? '正在读取诊断任务' : '从问题到有据可查的判断'}</h2><p>{detail.error || (selectedId ? '正在恢复任务状态、诊断和持久事件。' : '注册你的本机项目，提交问题，核对模型判断与来源，再批准需要执行的服务操作。')}</p>{!selectedId && <button className={s.primaryButton} type="button" onClick={openNew}><Icon name="plus" />创建第一个任务</button>}<div className={s.emptySteps}><span>01 提交问题</span><span>02 核对证据</span><span>03 人工决定</span></div></div> : <>
          <div className={s.detailHeader}><div><div className={s.breadcrumb}>{projects.find((project) => project.id === run.request.service_id)?.name || run.request.service_id} <Icon name="chevron" size={12} /><span>{run.request.mode === 'live' ? '真实本机诊断' : `Demo · ${run.request.incident_id}`}</span></div><h2>诊断与处置</h2><p className={s.subtle}>任务 <span className={s.mono}>#{shortId(run.id)}</span><span className={s.metaDot}>·</span>{time(run.created_at)} 提交</p></div><div className={s.detailActions}><Badge status={run.status} />{!isTerminal(run.status) && <button className={s.textButton} type="button" aria-label="取消当前任务" disabled={!!busy || run.cancel_requested} onClick={() => { void perform('cancel'); }}>{run.cancel_requested ? '已请求取消' : busy === 'cancel' ? '请求中…' : run.ticket_id ? '取消后续执行' : '取消任务'}</button>}</div></div>
          <div className={s.statusNote} data-state={run.status}><Icon name={run.status === 'SUCCEEDED' ? 'check' : run.status === 'FAILED' ? 'info' : 'clock'} size={17} /><p>{narrative(run)}</p><span className={s.connection} data-state={detail.connection}><i />{connectionLabel[detail.connection]}</span></div>
          {detail.error && <div className={s.inlineError} role="alert">连接暂时不可用：{detail.error}。正在恢复，已显示的结果仍保留。</div>}
          {run.status === 'FAILED' && run.error && <details className={s.errorDetails}><summary>查看错误详情</summary><code>{typeof run.error === 'string' ? run.error : `${run.error.type || run.error.code || 'ExecutionError'} · ${run.error.message || ''}`}</code></details>}
          <section className={s.questionCard} aria-labelledby="question-title"><span id="question-title" className={s.eyebrow}>提交的问题</span><p>{run.request.question}</p><div className={s.window}><Icon name="clock" size={14} />{time(run.request.start)} — {time(run.request.end)}<span>北京时间</span></div></section>

          <section className={s.reportCard} aria-labelledby="report-title">
            <div className={s.cardHeading}><div><span className={s.sectionNumber}>01</span><h3 id="report-title">辅助诊断</h3></div><span className={s.smallLabel}>模型判断 · 需结合证据核对</span></div>
            {run.diagnosis ? <><div className={s.summary}><span className={s.eyebrow}>当前结论</span><p>{run.diagnosis.summary}</p></div>
              <div className={s.reportSection}><h4>候选原因</h4>{run.diagnosis.hypotheses.length ? <ul className={s.plainList}>{run.diagnosis.hypotheses.map((hypothesis, index) => <li key={index}><span>{String(index + 1).padStart(2, '0')}</span><p>{hypothesis}</p></li>)}</ul> : <p className={s.muted}>当前证据不足以形成候选原因。</p>}</div>
              <div className={s.evidenceColumns}>{([
                ['支持证据', run.diagnosis.supporting_evidence, 'support'],
                ['反对 / 冲突证据', run.diagnosis.contradicting_evidence, 'against'],
              ] as const).map(([title, refs, tone]) => <div className={s.evidenceGroup} data-tone={tone} key={title}><h4><span />{title}<small>{refs.length}</small></h4>{refs.length ? refs.map((reference, index) => <div key={`${reference.id}:${index}`} className={s.quote}><p>“{reference.quote}”</p>{referenceButton(reference, index)}</div>) : <p className={s.emptyText}>{tone === 'support' ? '报告未列出支持引用。' : '报告未列出明确反证；这不表示反证不存在。'}</p>}</div>)}</div>
              <div className={s.nextColumns}><div><h4>仍缺少的信息</h4>{run.diagnosis.missing_information.length ? <ul className={s.bulletList}>{run.diagnosis.missing_information.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className={s.emptyText}>报告未列出额外缺失项。</p>}</div><div><h4>下一步检查</h4><ol className={s.checkList}>{run.diagnosis.recommended_checks.map((item, index) => <li key={index}><span>{index + 1}</span>{item}</li>)}</ol></div></div>
            </> : <div className={s.reportPending}><span className={s.pendingGlyph}><Icon name={run.status === 'FAILED' ? 'info' : 'file'} size={24} /></span><h4>{run.status === 'WAITING_APPROVAL' ? '先核对操作预览与引用' : run.status === 'CANCELLED' ? '本次未形成最终报告' : run.status === 'FAILED' ? '未生成有效的最终诊断' : '报告将在执行后显示'}</h4><p>{run.status === 'WAITING_APPROVAL' ? '任务停在人工批准边界。下方展示冻结操作，右侧可核对实际引用的来源；最终报告将在继续执行后显示。' : '这里仅展示服务端已完成的结构化结果，不推测尚未返回的诊断。'}</p></div>}
          </section>

          {!serviceAction && (draft || run.ticket_id || run.operation) && <section className={s.ticketCard} data-pending={pendingDecision} aria-labelledby="ticket-title"><div className={s.cardHeading}><div><span className={s.sectionNumber}>02</span><h3 id="ticket-title">人工交接</h3></div><span className={s.operationLabel} data-status={run.operation?.status}>{operationLabel}</span></div>
            {draft && <div className={s.draftBody}><div className={s.draftTitle}><h4>{draft.title}</h4><span className={s.priority}>{({ low: '低', medium: '中', high: '高' })[draft.priority]}优先级</span></div><p>{draft.description}</p>{run.operation && <div className={s.draftReferences}><span>草案 v{run.operation.draft_version} 的引用</span><div>{(run.operation.params.references || []).map((reference, index) => referenceButton(reference, index))}</div></div>}</div>}
            {run.ticket_id ? <div className={s.receipt}><span className={s.receiptIcon}><Icon name="check" size={19} /></span><div><strong>工单已实际创建</strong><p className={s.mono} aria-label="工单编号">{run.ticket_id}</p><small>服务端已保存同一业务操作的写入回执。</small></div></div>
              : pendingDecision ? <div className={s.approvalFooter}><p><Icon name="info" size={15} />批准仅授权当前冻结草案。此时尚未创建工单。</p><div><button className={s.secondaryButton} type="button" aria-label="拒绝当前工单草案" disabled={!!busy} onClick={() => { void perform('reject'); }}>{busy === 'reject' ? '记录中…' : '拒绝建单'}</button><button className={s.primaryButton} type="button" aria-label="批准当前工单草案" disabled={!!busy} onClick={() => { void perform('approve'); }}><Icon name="check" size={16} />{busy === 'approve' ? '记录中…' : '批准并创建工单'}</button></div></div>
              : <div className={s.ticketFootnote}>{run.operation?.decision === 'reject' ? '人工已拒绝此草案；未创建工单。' : run.operation?.decision === 'approve' ? isTerminal(run.status) ? '已有人工批准记录，但尚无工单回执；以任务终态与实际写入结果为准。' : '人工批准已记录。等待后台执行，取得回执后才显示为已建单。' : run.cancel_requested || isTerminal(run.status) ? '当前任务不再接受建单批准，尚无已创建工单。' : '这是诊断中的建议草案，尚未形成可批准的建单操作。'}</div>}
          </section>}

          {serviceAction && <ServiceActionPanel run={run} pending={!!pendingDecision} busy={busy} referenceButton={referenceButton} onDecision={(decision) => { void perform(decision); }} />}

          <section className={s.followupCard} aria-label="继续追问"><div><h3>继续核对与追问</h3><p>新任务保留本次上下文，继续采集和验证，不覆盖当前报告。</p>{run.request.parent_run_id && <button type="button" className={s.textButton} onClick={() => selectRun(run.request.parent_run_id!)}>查看父任务 #{shortId(run.request.parent_run_id)}</button>}</div><button type="button" className={s.secondaryButton} disabled={!isTerminal(run.status)} onClick={() => { setFollowupParent(run); setNewOpen(true); }}>基于此任务追问<Icon name="arrow" size={16} /></button>{!isTerminal(run.status) && <small>本次任务结束后可继续追问。</small>}</section>

          <section className={s.usageCard} aria-labelledby="usage-title"><div className={s.usageHeading}><h3 id="usage-title">调用与用量</h3><span>持久账本 · 费用为估算</span></div><div className={s.usageGrid}><div><span>模型调用</span><strong>{run.usage.model_calls}<small>次</small></strong></div><div><span>工具调用</span><strong>{run.tool_calls}<small>次</small></strong></div><div><span>已核账 tokens</span><strong>{(run.usage.input_tokens + run.usage.output_tokens).toLocaleString()}</strong></div><div><span>已知估算费</span><strong className={s.cost}>{fee(run.usage.known_cost_cny)}</strong></div></div><div className={s.usageFoot}><span>活跃执行 {run.active_seconds.toFixed(2)} 秒{run.active_started_at ? '（本段执行中）' : ''} · 缓存命中 {run.usage.cache_read_tokens.toLocaleString()} tokens</span><span>{run.usage.unknown_usage_calls > 0 ? `${run.usage.unknown_usage_calls} 次用量未知 · 预留 ${fee(run.usage.unreconciled_reservations_cny)}` : '无未核账预留'}</span></div>{(run.report_repairs || 0) > 0 && <p className={s.repairNote}>报告修正 {run.report_repairs} 次。这些请求计入上方模型调用、tokens 与费用，并受同一任务预算限制。</p>}</section>

          <section className={s.timelineCard} aria-labelledby="events-title"><div className={s.usageHeading}><h3 id="events-title">关键事件</h3><span>可观察的业务动作</span></div>{detail.events.length ? <ol className={s.timeline}>{detail.events.slice(-12).map((event) => <li key={event.event_id}><span className={s.timelineDot} /><div><strong>{eventLabel(event)}</strong><span>{time(event.created_at, true)}<small>#{event.event_id}</small></span></div></li>)}</ol> : <p className={s.emptyText}>正在按持久 cursor 补读事件。</p>}<p className={s.timelineNote}>仅展示已记录的阶段与操作，不展示内部思维过程。{detail.events.length > 12 ? `最近 12 条，共缓存 ${detail.events.length} 条事件。` : ''}</p></section>
          <footer className={s.detailFooter}><span>{run.request.mode === 'live' ? '真实本机证据' : 'Demo · 固定模拟证据'} · 辅助诊断由人工核对</span><span className={s.mono} title={run.id}>#{shortId(run.id)}</span></footer>
        </>}
      </section>

      {compact && activeReference && <button type="button" className={s.drawerBackdrop} aria-label="关闭来源证据遮罩" onClick={closeEvidence} />}
      <aside className={s.evidencePanel} data-open={!!activeReference} aria-label="来源证据" role={compact && activeReference ? 'dialog' : 'complementary'} aria-modal={compact && activeReference ? true : undefined} ref={evidencePanel} tabIndex={-1} onKeyDown={evidenceKeyDown}>
        <div className={s.evidenceHeading}><div><span className={s.eyebrow}>SOURCE OF TRUTH</span><h3><Icon name="evidence" size={18} />来源证据<span className={s.count}>{references.length}</span></h3></div>{activeReference && <button type="button" className={s.iconButton} aria-label="关闭来源证据" onClick={closeEvidence}><Icon name="close" size={17} /></button>}</div>
        {activeReference ? <><div className={s.sourceIdentity}><span className={s.mono}>{activeReference.id}</span><span>版本 {activeReference.version}</span></div><div className={s.sourceQuote}><span className={s.eyebrow}>报告 / 草案引用</span><p>“{activeReference.quote}”</p></div>{snapshotError ? <div className={s.inlineError} role="alert">{snapshotError}</div> : visibleSnapshot ? <div className={s.snapshot}><div className={s.snapshotHeading}><h4>本次取得的快照</h4><span>{visibleSnapshot.source || (visibleSnapshot.doc_id ? '排障规范' : '运行证据')}</span></div><dl>{visibleSnapshot.service_id && <><dt>服务</dt><dd>{visibleSnapshot.service_id}</dd></>}{visibleSnapshot.doc_id && <><dt>文档</dt><dd>{visibleSnapshot.doc_id}</dd></>}{snapshotTime && <><dt>记录时间</dt><dd>{time(snapshotTime, true)}</dd></>}<dt>版本</dt><dd>{String(visibleSnapshot.version)}</dd></dl><pre aria-label="证据快照原文">{typeof visibleSnapshot.content === 'string' ? visibleSnapshot.content : JSON.stringify(visibleSnapshot.content, null, 2)}</pre><p className={s.sourceNote}><Icon name="info" size={14} />引用可以解析不等于已经证明因果；请结合正文与反证判断。</p></div> : <p className={s.loadingSource}>正在读取该任务的持久快照…</p>}</> : <div className={s.sourceEmpty}><span><Icon name="file" size={27} /></span><h4>结论值得被核对</h4><p>点击报告或操作预览中的引用，在这里查看当前任务实际取得的原文与版本。</p></div>}
        {references.length > 0 && <div className={s.referenceIndex}><h4>本任务引用索引</h4>{references.map((reference, index) => <div key={`${reference.id}:${index}`}>{referenceButton(reference, index)}<span title={`完整版本：${reference.version}`} aria-label={`完整版本 ${reference.version}`}>v{shortVersion(reference.version)}</span></div>)}</div>}
        <div className={s.sourceBottom}><span className={s.dot} />仅显示本任务已取得的来源</div>
      </aside>
    </main>
    <SettingsDialog open={settingsOpen} tab={settingsTab} projects={projects} modelSettings={modelSettings} loadingError={settingsError} onClose={() => setSettingsOpen(false)} onReload={() => { void loadSettings(); }} onProjectSaved={(value) => { setProjects((previous) => [...previous.filter((item) => item.id !== value.id), value]); void loadReadiness(); }} onModelSaved={(value) => { setModelSettings(value); void loadReadiness(); }} />
    <NewRunDialog open={newOpen} incidents={incidents} projects={projects} modelSettings={modelSettings} parent={followupParent} onSettings={() => { setNewOpen(false); openSettings('projects'); }} onClose={() => setNewOpen(false)} onSubmitted={(created) => { updateRun(created); selectRun(created.id); setToast({ message: '诊断任务已持久提交。你可以留在这里查看进度，也可以稍后回来。', error: false }); }} />
  </div>;
}
