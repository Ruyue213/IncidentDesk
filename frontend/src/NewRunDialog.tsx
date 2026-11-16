import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError } from './api.ts';
import { composeQuestion, readStore, removeStore, restoreQuestion, submissionIntent, writeStore } from './state.ts';
import type { SubmissionIntent } from './state.ts';
import type { Incident, ModelSettings, ProjectProfile, Run, RunInput } from './types.ts';
import Icon from './Icon.tsx';
import s from './App.module.css';

function beijingInput(iso: string) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(iso));
  const value = (type: string) => parts.find((part) => part.type === type)?.value;
  return `${value('year')}-${value('month')}-${value('day')}T${value('hour')}:${value('minute')}`;
}
function toIso(value: string) { return new Date(`${value}:00+08:00`).toISOString(); }
const PENDING_KEY = 'incidentdesk.pendingSubmit';

export default function NewRunDialog({ open, incidents, projects, modelSettings, parent, onClose, onSettings, onSubmitted }: {
  open: boolean; incidents: Incident[]; projects: ProjectProfile[]; modelSettings: ModelSettings | null; parent: Run | null;
  onClose: () => void; onSettings: () => void; onSubmitted: (run: Run) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const sending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<'live' | 'fixture'>('live');
  const [serviceId, setServiceId] = useState('');
  const [incidentId, setIncidentId] = useState('');
  const [projectVersion, setProjectVersion] = useState<number | null>(null);
  const [modelVersion, setModelVersion] = useState<number | null>(null);
  const [question, setQuestion] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [ticket, setTicket] = useState(false);
  const [error, setError] = useState('');
  const [restored, setRestored] = useState(false);
  const pendingKey = parent ? `${PENDING_KEY}.${parent.id}` : PENDING_KEY;
  const currentProject = projects.find((item) => item.id === serviceId);

  const recentWindow = (minutes = 30) => {
    const now = Math.floor(Date.now() / 60000) * 60000;
    setStart(beijingInput(new Date(now - minutes * 60000).toISOString())); setEnd(beijingInput(new Date(now).toISOString()));
  };
  const selectIncident = (incident: Incident) => {
    setIncidentId(incident.incident_id || ''); setServiceId(incident.service_id); setQuestion(incident.question);
    setStart(beijingInput(incident.start)); setEnd(beijingInput(incident.end)); setProjectVersion(null); setError(''); setRestored(false);
  };
  useEffect(() => {
    if (!open) { dialog.current?.close(); return; }
    setError(''); setTicket(false); setRestored(false); setQuestion('');
    const previous = readStore<SubmissionIntent>(localStorage, pendingKey);
    if (previous?.key && previous.body && (previous.body.parent_run_id || null) === (parent?.id || null)) {
      const body = previous.body; const restoredQuestion = restoreQuestion(body.question);
      setMode(body.mode || 'fixture'); setServiceId(body.service_id); setIncidentId(body.incident_id || '');
      setProjectVersion(body.project_version ?? null); setModelVersion(body.model_profile_version ?? null);
      setQuestion(body.mode === 'live' ? body.question : restoredQuestion.question);
      setTicket(body.mode === 'live' ? false : restoredQuestion.ticket);
      setStart(beijingInput(body.start)); setEnd(beijingInput(body.end)); setRestored(true);
    } else {
      const initialMode = parent?.request.mode || (parent ? 'fixture' : 'live');
      setMode(initialMode); setModelVersion(modelSettings?.active_version ?? null);
      if (initialMode === 'fixture') {
        const incident = incidents.find((item) => item.incident_id === parent?.request.incident_id) || incidents[0];
        if (incident) selectIncident(incident);
        if (parent) setQuestion('');
      } else {
        const project = projects.find((item) => item.id === parent?.request.service_id) || projects[0];
        setServiceId(project?.id || ''); setProjectVersion(project?.version ?? null); setIncidentId(''); recentWindow();
      }
    }
    if (!dialog.current?.open) dialog.current?.showModal();
    // Capture versions only when opening; a background settings refresh must not replace edits.
  }, [open, parent?.id]);
  useEffect(() => {
    if (!open || restored) return;
    if (mode === 'live' && !serviceId && projects[0]) { setServiceId(projects[0].id); setProjectVersion(projects[0].version); }
    if (modelVersion === null && modelSettings) setModelVersion(modelSettings.active_version);
  }, [open, restored, mode, serviceId, projects, modelVersion, modelSettings]);

  async function submit(event: FormEvent) {
    event.preventDefault(); if (sending.current) return;
    sending.current = true; setBusy(true); setError('');
    try {
      const previous = readStore<SubmissionIntent>(localStorage, pendingKey);
      const sameDisplayedTime = (field: 'start' | 'end', value: string) => previous?.body?.[field] && beijingInput(previous.body[field]) === value ? previous.body[field] : toIso(value);
      const body: RunInput = { mode, question: composeQuestion(question, mode === 'fixture' && ticket), service_id: serviceId,
        ...(mode === 'fixture' ? { incident_id: incidentId } : { incident_id: null, project_version: projectVersion }),
        model_profile_version: modelVersion, parent_run_id: parent?.id || null,
        start: sameDisplayedTime('start', start), end: sameDisplayedTime('end', end) };
      if (body.question.length < 5 || body.question.length > 4000) throw new Error('问题需为 5–4000 个字符（含工单申请说明）。');
      if (!serviceId || (mode === 'fixture' && !incidentId)) throw new Error('请选择项目或模拟案例。');
      if (new Date(body.start) >= new Date(body.end)) throw new Error('结束时间必须晚于开始时间。');
      const intent = submissionIntent(body, previous, () => crypto.randomUUID());
      writeStore(localStorage, pendingKey, intent);
      const run = await api.submit(intent.body, intent.key);
      removeStore(localStorage, pendingKey); onSubmitted(run); onClose();
    } catch (failure) {
      setError(failure instanceof ApiError && failure.status === 409 ? '配置版本或提交标识已发生冲突。请核对项目与模型设置后重新提交。'
        : failure instanceof Error ? `${failure.message} 如结果未确认，保留当前内容重试会复用同一请求。` : '提交未确认，请保留当前内容重试。');
    } finally { sending.current = false; setBusy(false); }
  }

  return <dialog className={s.dialog} ref={dialog} aria-labelledby="new-run-title" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <div className={s.dialogHeading}><div><span className={s.eyebrow}>{parent ? `继续任务 #${parent.id.slice(0, 8)}` : '开始一次诊断'}</span><h2 id="new-run-title">{parent ? '基于已有证据继续追问' : '让问题与证据对齐'}</h2></div><button type="button" className={s.iconButton} aria-label="关闭新建任务" onClick={onClose}><Icon name="close" /></button></div>
    <form onSubmit={(event) => { void submit(event); }} className={s.form}>
      <p className={s.muted}>{parent ? '新任务保留父任务上下文，并可在新的时间窗继续采集。原任务和报告会完整保留。' : '选择真实项目，描述你想查清的问题。任务在后台执行，关闭页面不会取消任务。'}</p>
      {!parent && <label>任务来源<select aria-label="任务来源" value={mode} onChange={(event) => {
        const next = event.target.value as 'live' | 'fixture'; setMode(next); setRestored(false); setTicket(false);
        if (next === 'fixture' && incidents[0]) selectIncident(incidents[0]);
        else { const project = projects[0]; setServiceId(project?.id || ''); setProjectVersion(project?.version ?? null); setIncidentId(''); setQuestion(''); recentWindow(); }
      }}><option value="live">真实本机项目</option><option value="fixture">Demo · 固定模拟案例</option></select></label>}
      {mode === 'fixture' ? <><div className={s.inlineInfo}>模拟模式仅使用固定案例证据，不代表你的本机服务状态。</div><label>案例<select aria-label="选择模拟案例" value={incidentId} required disabled={!!parent} onChange={(event) => { const selected = incidents.find((item) => item.incident_id === event.target.value); if (selected) selectIncident(selected); }}>
        {!incidents.length && <option value="">暂无可用模拟案例</option>}{incidents.map((incident) => <option key={incident.incident_id} value={incident.incident_id || ''}>{incident.incident_id} · {incident.question.slice(0, 36)}</option>)}
      </select></label></> : <><label>真实项目<select aria-label="真实项目" value={serviceId} required disabled={!!parent} onChange={(event) => { const project = projects.find((item) => item.id === event.target.value); setServiceId(event.target.value); setProjectVersion(project?.version ?? null); setRestored(false); }}>
        {!projects.length && <option value="">请先注册本机项目</option>}{serviceId && !currentProject && <option value={serviceId}>{serviceId} · 历史配置</option>}{projects.map((project) => <option key={project.id} value={project.id}>{project.name} · v{project.version}</option>)}
      </select></label>{!projects.length ? <button type="button" className={s.secondaryButton} onClick={onSettings}>注册真实项目</button> : <p className={s.muted}>{currentProject?.root || serviceId} · 绑定项目 v{projectVersion ?? '未确认'} · 模型 v{modelVersion ?? '服务端默认'}</p>}
      <div className={s.windowPresets}><span>采集时间窗</span>{[15, 30, 60, 180].map((minutes) => <button type="button" key={minutes} onClick={() => recentWindow(minutes)}>最近 {minutes < 60 ? `${minutes} 分钟` : `${minutes / 60} 小时`}</button>)}</div></>}
      <label>{parent ? '追问内容' : '异常描述'}<textarea aria-label="异常描述" minLength={5} maxLength={4000} required value={question} placeholder={parent ? '例如：核对重启后的错误率，原来的判断是否仍然成立？' : '例如：服务响应变慢，请检查最近的日志、进程和配置，定位可能原因并说明证据。'} onChange={(event) => setQuestion(event.target.value)} rows={4} /></label>
      <div className={s.timeFields}><label>开始时间（北京时间）<input aria-label="开始时间" type="datetime-local" required value={start} onChange={(event) => setStart(event.target.value)} /></label><label>结束时间（北京时间）<input aria-label="结束时间" type="datetime-local" required value={end} onChange={(event) => setEnd(event.target.value)} /></label></div>
      {mode === 'fixture' && <label className={s.checkboxCard}><input aria-label="申请人工交接工单" type="checkbox" checked={ticket} onChange={(event) => setTicket(event.target.checked)} /><span><strong>需要人工交接工单</strong><small>先展示冻结草案，只有你明确批准后才会创建。</small></span></label>}
      {mode === 'live' && <p className={s.muted}>诊断读取允许的本机证据；重启与修改配置须另行核对操作预览并批准。</p>}
      {restored && <div className={s.inlineInfo}>已恢复上次未确认的提交及其配置版本。保留原内容重试将复用同一请求标识。</div>}
      {error && <div className={s.inlineError} role="alert">{error}</div>}
      <div className={s.dialogFooter}><button type="button" className={s.secondaryButton} onClick={onClose}>暂不提交</button><button type="submit" className={s.primaryButton} disabled={busy || !serviceId} aria-label="提交诊断任务">{busy ? '正在提交…' : parent ? '提交追问任务' : '提交诊断任务'}<Icon name="arrow" size={16} /></button></div>
    </form>
  </dialog>;
}
