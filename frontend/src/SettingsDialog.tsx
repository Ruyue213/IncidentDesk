import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api.ts';
import type { ModelInput, ModelSettings, ModelTest, ProjectInput, ProjectProbe, ProjectProfile } from './types.ts';
import Icon from './Icon.tsx';
import ProjectEvidencePanel from './ProjectEvidencePanel.tsx';
import s from './App.module.css';

const lines = (value: string) => value.split('\n').map((item) => item.trim()).filter(Boolean);
const failureText = (failure: unknown) => failure instanceof Error ? failure.message : '操作未完成，请稍后重试。';
const sourceName: Record<string, string> = { logs: '日志', health: '健康检查', metrics: '指标', stats: '业务统计', process: '本地进程', changes: '配置与变更' };
const sourceStatus: Record<string, string> = { complete: '已采集', partial: '部分可用', unavailable: '不可用' };

function ProjectEditor({ project, onSaved }: { project: ProjectProfile | null; onSaved: (profile: ProjectProfile) => void }) {
  const [id, setId] = useState(project?.id || '');
  const [name, setName] = useState(project?.name || '');
  const [root, setRoot] = useState(project?.root || '');
  const [logs, setLogs] = useState(project?.log_files.join('\n') || '');
  const [configs, setConfigs] = useState(project?.config_files.join('\n') || '');
  const [knowledgePaths, setKnowledgePaths] = useState(project?.knowledge_paths?.join('\n') || '');
  const [retrievalMode, setRetrievalMode] = useState(project?.retrieval_mode || 'hybrid');
  const [historyEnabled, setHistoryEnabled] = useState(project?.history_enabled !== false);
  const [businessProbe, setBusinessProbe] = useState(project?.business_probe ? JSON.stringify(project.business_probe, null, 2) : '');
  const [argv, setArgv] = useState(JSON.stringify(project?.start_argv || [], null, 2));
  const [health, setHealth] = useState(project?.health_url || '');
  const [metrics, setMetrics] = useState(project?.metrics_url || '');
  const [stats, setStats] = useState(project?.stats_url || '');
  const [busy, setBusy] = useState('');
  const action = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [probe, setProbe] = useState<ProjectProbe | null>(null);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (action.current) return;
    action.current = true; setBusy('save'); setError(''); setNotice('');
    try {
      const startArgv: unknown = JSON.parse(argv);
      if (!Array.isArray(startArgv) || !startArgv.every((part) => typeof part === 'string')) throw new Error('启动参数必须为 JSON 字符串数组，例如 ["python", "app.py"]。');
      const body: ProjectInput = { id: id.trim(), name: name.trim(), root: root.trim(), log_files: lines(logs), config_files: lines(configs), start_argv: startArgv,
        knowledge_paths: lines(knowledgePaths), retrieval_mode: retrievalMode, history_enabled: historyEnabled,
        business_probe: businessProbe.trim() ? JSON.parse(businessProbe) : null,
        health_url: health.trim() || null, metrics_url: metrics.trim() || null, stats_url: stats.trim() || null,
        timezone: project?.timezone || 'Asia/Shanghai', ...(project ? { expected_version: project.version } : {}) };
      const saved = await api.saveProject(body.id, body);
      setProbe(null); onSaved(saved); setNotice(`已保存项目 v${saved.version}。新任务使用此版本；历史任务保留原配置。`);
    } catch (failure) { setError(failureText(failure)); }
    finally { action.current = false; setBusy(''); }
  }
  async function inspect() {
    if (!project || action.current) return;
    action.current = true; setBusy('probe'); setError(''); setProbe(null);
    try { setProbe(await api.probeProject(project.id)); }
    catch (failure) { setError(failureText(failure)); }
    finally { action.current = false; setBusy(''); }
  }
  return <form className={s.form} onSubmit={(event) => { void save(event); }}>
    <p className={s.muted}>注册本机服务及允许读取的来源。日志、配置与启动参数须与你的真实进程一致；保存配置不会启动或重启服务。</p>
    <div className={s.timeFields}><label>项目标识<input aria-label="项目标识" required pattern="[a-zA-Z0-9_-]+" value={id} disabled={!!project} onChange={(event) => setId(event.target.value)} placeholder="socialengine" /></label><label>项目名称<input aria-label="项目名称" required value={name} onChange={(event) => setName(event.target.value)} placeholder="SocialEngine" /></label></div>
    <label>本机项目目录<input aria-label="本机项目目录" required value={root} onChange={(event) => setRoot(event.target.value)} placeholder="/Users/…/SocialEngine" /></label>
    <div className={s.timeFields}><label>日志文件（每行一个）<textarea aria-label="日志文件" value={logs} onChange={(event) => setLogs(event.target.value)} rows={3} placeholder="logs/app.log" /></label><label>允许修改的配置文件（每行一个）<textarea aria-label="配置文件" value={configs} onChange={(event) => setConfigs(event.target.value)} rows={3} placeholder="config/settings.json" /></label></div>
    <label>启动参数（JSON 数组）<textarea className={s.codeInput} aria-label="启动参数" value={argv} onChange={(event) => setArgv(event.target.value)} rows={3} spellCheck={false} /><small>逐项填写程序与参数，不填写 shell 拼接命令或密钥。只有人工批准服务动作后才会使用。</small></label>
    <label>健康检查地址（可选）<input aria-label="健康检查地址" value={health} onChange={(event) => setHealth(event.target.value)} placeholder="http://127.0.0.1:8000/health" /></label>
    <div className={s.timeFields}><label>指标地址（可选）<input aria-label="指标地址" value={metrics} onChange={(event) => setMetrics(event.target.value)} /></label><label>业务统计地址（可选）<input aria-label="业务统计地址" value={stats} onChange={(event) => setStats(event.target.value)} /></label></div>
    <label>知识文档（每行一个相对路径，可选）<textarea aria-label="知识文档" value={knowledgePaths} onChange={(event) => setKnowledgePaths(event.target.value)} rows={3} placeholder="docs/runbooks\ndocs/architecture.md" /><small>只登记已审核的 Markdown / 文本。保存后手动建立索引；不要登记凭证或未脱敏的业务材料。</small></label>
    <label>知识检索方式<select aria-label="知识检索方式" value={retrievalMode} onChange={(event) => setRetrievalMode(event.target.value as typeof retrievalMode)}><option value="hybrid">混合 · BM25 + 向量 + RRF</option><option value="lexical">词法 · BM25</option><option value="dense">向量 · 语义检索</option></select></label>
    <label className={s.checkboxCard}><input type="checkbox" checked={historyEnabled} onChange={(event) => setHistoryEnabled(event.target.checked)} /><span>保留本机运行观测历史<small>后台每 10 秒采集已登记健康、指标与统计入口，最多保留 7 天；未开始采集的时间段没有历史数据。</small></span></label>
    <label>固定只读业务探针（JSON，可选）<textarea aria-label="业务探针配置" className={s.codeInput} value={businessProbe} onChange={(event) => setBusinessProbe(event.target.value)} rows={5} spellCheck={false} placeholder={'{"method":"POST","path":"/query","body":{"type":"dist","a":1,"b":1},"expected_status":200,"expected_json":{"distance":0}}'} /><small>请求只发送到健康检查地址的同一来源。当前支持登记的只读查询及 JSON 断言；Agent 不能自造 URL 或请求体。</small></label>
    {error && <div className={s.inlineError} role="alert">{error}</div>}{notice && <div className={s.inlineInfo} role="status">{notice}</div>}
    <div className={s.dialogFooter}>{project && <button type="button" className={s.secondaryButton} disabled={!!busy} onClick={() => { void inspect(); }}>{busy === 'probe' ? '正在采集…' : '检查已保存项目的采集状态'}</button>}<button className={s.primaryButton} disabled={!!busy} type="submit">{busy === 'save' ? '正在保存…' : '保存项目配置'}</button></div>
    {probe && <section className={s.probeResults} aria-label="采集检查结果"><h4>项目 v{probe.project_version} · 本次实际采集</h4><p className={s.muted}>结果仅表示本次检查，不代表之后的诊断一定能获得完整证据。</p>{probe.items.map((item) => <div key={item.source}><strong>{sourceName[item.source] || item.source}</strong><span>{sourceStatus[item.status] || item.status} · {item.items.length} 条</span>{item.error && <p>{item.error}</p>}</div>)}</section>}
    {project && <ProjectEvidencePanel key={`${project.id}:${project.version}`} project={project} />}
  </form>;
}

function ModelEditor({ settings, onSaved }: { settings: ModelSettings; onSaved: (value: ModelSettings) => void }) {
  const { version, has_credential, experimental: _experimental, ...initial } = settings.profile;
  const [form, setForm] = useState<ModelInput>({ ...initial, expected_version: version });
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState('');
  const action = useRef(false);
  const [error, setError] = useState('');
  const [models, setModels] = useState<string[]>([]);
  const [result, setResult] = useState<ModelTest | null>(null);
  const [notice, setNotice] = useState('');
  const update = <K extends keyof ModelInput>(field: K, value: ModelInput[K]) => setForm((previous) => ({ ...previous, [field]: value }));
  const changed = JSON.stringify({ ...form, expected_version: undefined }) !== JSON.stringify(initial) || !!key || clearKey;
  async function save(event: FormEvent) {
    event.preventDefault(); if (action.current) return;
    action.current = true; setBusy('save'); setError('');
    try {
      const saved = await api.saveModel({ ...form, ...(key.trim() ? { api_key: key.trim() } : {}), ...(clearKey ? { clear_credential: true } : {}) });
      setKey(''); setClearKey(false); onSaved(saved);
    } catch (failure) { setError(failureText(failure)); }
    finally { action.current = false; setBusy(''); }
  }
  async function check(kind: 'models' | 'test') {
    if (action.current || changed) return;
    action.current = true; setBusy(kind); setError(''); setNotice(''); setResult(null);
    try {
      if (kind === 'models') { const response = await api.models(version); setModels(response.models.map((item) => item.id)); setNotice(`已读取 ${response.models.length} 个模型；未发起推理调用。`); }
      else setResult(await api.testModel(version));
    } catch (failure) { setError(failureText(failure)); }
    finally { action.current = false; setBusy(''); }
  }
  const numeric: [keyof Pick<ModelInput, 'max_output_tokens' | 'request_timeout_seconds' | 'active_seconds' | 'model_call_limit' | 'tool_call_limit' | 'run_budget_cny' | 'project_budget_cny'>, string, number, number][] = [
    ['max_output_tokens', '单次最大输出 tokens', 128, 16384], ['request_timeout_seconds', '单次请求超时（秒）', 1, 180], ['active_seconds', '任务活跃时间上限（秒）', 10, 1800], ['model_call_limit', '任务模型调用上限', 2, 64], ['tool_call_limit', '任务工具调用上限', 1, 128], ['run_budget_cny', '单任务预算（元）', 0.01, 50], ['project_budget_cny', '项目总预算（元）', 0.01, 300],
  ];
  return <form className={s.form} onSubmit={(event) => { void save(event); }}>
    <div className={s.settingsNotice}><strong>当前保存版本 v{version}</strong><span>{has_credential ? 'API 密钥已配置 · 内容不回显' : '尚未配置 API 密钥'}</span></div>
    <label>服务地址<input aria-label="模型服务地址" required type="url" value={form.base_url} onChange={(event) => update('base_url', event.target.value)} /><small>更换服务域名时须填写新密钥，或选择清除密钥。密钥不会自动转发到新域名。</small></label>
    <div className={s.timeFields}><label>模型 ID<input aria-label="模型 ID" required value={form.model} list="available-models" onChange={(event) => update('model', event.target.value)} /><datalist id="available-models">{models.map((id) => <option key={id} value={id} />)}</datalist></label><label>推理强度<select aria-label="推理强度" value={form.reasoning} onChange={(event) => update('reasoning', event.target.value as ModelInput['reasoning'])}>{settings.capabilities.reasoning.map((item) => <option key={item} value={item}>{({ off: 'off · 关闭', low: 'low · 低', high: 'high · 高', max: 'max · 最高' })[item]}</option>)}</select></label></div>
    <label>{has_credential ? '替换 API 密钥（留空保留）' : 'API 密钥'}<input aria-label="API 密钥" type="password" autoComplete="new-password" value={key} disabled={clearKey} onChange={(event) => setKey(event.target.value)} placeholder={has_credential ? '已配置，内容隐藏' : '仅写入本机，不在页面回显'} /><small>仅发送给本机后端保存；不写入浏览器存储。保存成功或关闭设置后清空输入。</small></label>
    {has_credential && <label className={s.checkboxCard}><input type="checkbox" checked={clearKey} onChange={(event) => { setClearKey(event.target.checked); setKey(''); }} /><span>清除新配置版本的密钥<small>已有任务仍保留其绑定的历史配置。</small></span></label>}
    <details className={s.advancedSettings}><summary>调用限制、预算与计价</summary><div className={s.timeFields}>{numeric.map(([field, label, min, max]) => <label key={field}>{label}<input type="number" required min={min} max={max} step={field.endsWith('_cny') ? '0.01' : '1'} value={form[field]} onChange={(event) => update(field, Number(event.target.value))} /></label>)}</div><div className={s.timeFields}>{([['input_per_million_cny', '输入 / 百万 tokens（元）'], ['output_per_million_cny', '输出 / 百万 tokens（元）'], ['cache_read_per_million_cny', '缓存读取 / 百万 tokens（元）']] as const).map(([field, label]) => <label key={field}>{label}<input type="number" required min="0" step="0.001" value={form.pricing[field]} onChange={(event) => update('pricing', { ...form.pricing, [field]: Number(event.target.value) })} /></label>)}</div><p className={s.muted}>计价用于本机预算估算，请以供应商当前价格和账单为准。</p></details>
    {settings.profile.experimental && <div className={s.inlineInfo}>当前模型或推理组合尚待连接验证；保存配置不表示模型已就绪。</div>}
    {error && <div className={s.inlineError} role="alert">{error}</div>}{notice && <div className={s.inlineInfo} role="status">{notice}</div>}
    <div className={s.dialogFooter}><button type="submit" className={s.primaryButton} disabled={!!busy}>{busy === 'save' ? '正在保存…' : '保存模型设置'}</button></div>
    <section className={s.connectionTest} aria-label="模型连接验证"><h4>验证已保存的版本 v{version}</h4><p className={s.muted}>先保存再验证。连接验证会向云端发送测试内容，最多两次模型调用并计入预算；不会修改业务服务。</p>{changed && <p className={s.muted}>当前有未保存内容，保存后才能验证。</p>}<div className={s.buttonRow}><button type="button" className={s.secondaryButton} disabled={!!busy || changed || !has_credential} onClick={() => { void check('models'); }}>{busy === 'models' ? '正在读取…' : '读取可用模型（无推理）'}</button><button type="button" className={s.secondaryButton} disabled={!!busy || changed || !has_credential} onClick={() => { void check('test'); }}>{busy === 'test' ? '正在验证…' : '验证连接与工具调用（计费）'}</button></div>{result && <div className={result.ok ? s.inlineInfo : s.inlineError} role="status"><strong>{result.ok ? '连接与工具回合验证通过' : '验证未通过'} · v{result.version}</strong><p>{result.message}</p><p>请求模型：{result.requested_model} · 返回模型：{result.returned_model || '未确认'}<br />模型调用 {result.model_calls} 次 · 已知估算 ¥{result.known_cost_cny.toFixed(6)} · 未核账预留 ¥{result.unreconciled_reservations_cny.toFixed(6)}{result.error_type ? ` · ${result.error_type}` : ''}</p></div>}</section>
  </form>;
}

export default function SettingsDialog({ open, tab, projects, modelSettings, loadingError, onClose, onReload, onProjectSaved, onModelSaved }: {
  open: boolean; tab: 'projects' | 'model'; projects: ProjectProfile[]; modelSettings: ModelSettings | null; loadingError: string;
  onClose: () => void; onReload: () => void; onProjectSaved: (value: ProjectProfile) => void; onModelSaved: (value: ModelSettings) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [active, setActive] = useState(tab);
  const [selected, setSelected] = useState('');
  useEffect(() => { if (open) { setActive(tab); setSelected(projects[0]?.id || ''); if (!dialog.current?.open) dialog.current?.showModal(); } else dialog.current?.close(); }, [open, tab]);
  const project = projects.find((item) => item.id === selected) || null;
  return <dialog ref={dialog} className={`${s.dialog} ${s.settingsDialog}`} aria-labelledby="settings-title" onCancel={(event) => { event.preventDefault(); onClose(); }}><div className={s.dialogHeading}><div><span className={s.eyebrow}>本机工作台</span><h2 id="settings-title">项目与模型设置</h2></div><button type="button" className={s.iconButton} aria-label="关闭设置" onClick={onClose}><Icon name="close" /></button></div><div className={s.settingsTabs} role="tablist" aria-label="设置分类"><button role="tab" type="button" aria-selected={active === 'projects'} onClick={() => setActive('projects')}>项目与采集</button><button role="tab" type="button" aria-selected={active === 'model'} onClick={() => setActive('model')}>模型与预算</button></div>
    {open && <div className={s.settingsContent}>{loadingError && <div className={s.inlineError} role="alert">{loadingError}<button className={s.textButton} type="button" onClick={onReload}>重新读取</button></div>}{active === 'projects' ? <><div className={s.projectPicker}><label>编辑项目<select aria-label="编辑项目" value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">＋ 注册新项目</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name} · v{item.version}</option>)}</select></label></div><ProjectEditor key={`${project?.id || 'new'}:${project?.version || 0}`} project={project} onSaved={(value) => { onProjectSaved(value); setSelected(value.id); }} /></> : modelSettings ? <ModelEditor key={modelSettings.active_version} settings={modelSettings} onSaved={onModelSaved} /> : <p className={s.muted}>正在读取模型设置。若长时间未返回，请检查本机 API 后重试。</p>}</div>}
  </dialog>;
}
