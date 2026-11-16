import { useEffect, useRef, useState } from 'react';
import { api } from './api.ts';
import type { EvidenceBatch, EvidenceSnapshot, KnowledgeStatus, ProjectProfile } from './types.ts';
import s from './App.module.css';

const failureText = (value: unknown) => value instanceof Error ? value.message : '读取失败，请重试。';
const modelState: Record<string, string> = { ready: '本机可用', missing_dependencies: '缺少计算依赖', not_installed: '尚未安装' };

function sourceLocation(item: EvidenceSnapshot) {
  const path = item.provenance?.relative_path;
  if (!path) return item.id;
  const first = item.provenance?.line_start;
  const last = item.provenance?.line_end;
  return first ? `${path}:${first}${last && last !== first ? `–${last}` : ''}` : path;
}

function retrievalError(error: EvidenceBatch['error']) {
  if (!error) return '';
  if (typeof error === 'string') return error;
  if (error.code === 'embedding_unavailable') return '本次知识版本的向量检索不可用，已降级为词法检索。请检查本机模型并重新建立索引。';
  return error.message || '本次检索未获得完整结果。';
}

export default function ProjectEvidencePanel({ project }: { project: ProjectProfile }) {
  const [knowledge, setKnowledge] = useState<KnowledgeStatus | null>(null);
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<EvidenceBatch | null>(null);
  const [history, setHistory] = useState<EvidenceBatch | null>(null);
  const [probe, setProbe] = useState<EvidenceBatch | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const epoch = useRef(0);
  const action = useRef<number | null>(null);
  const scope = `${project.id}:${project.version}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;

  useEffect(() => {
    const token = ++epoch.current;
    action.current = null;
    setKnowledge(null); setQuery(''); setResult(null); setHistory(null); setProbe(null);
    setError(''); setNotice(''); setBusy('');
    void api.knowledge(project.id).then((value) => {
      if (epoch.current === token && activeScope.current === scope) setKnowledge(value);
    }).catch((failure) => {
      if (epoch.current === token && activeScope.current === scope) setError(failureText(failure));
    });
    return () => { if (epoch.current === token) epoch.current++; };
  }, [project.id, project.version, scope]);

  async function run(kind: 'index' | 'search' | 'history' | 'probe') {
    if (action.current !== null) return;
    const token = epoch.current;
    const current = () => epoch.current === token && activeScope.current === scope;
    action.current = token; setBusy(kind); setError(''); setNotice('');
    try {
      if (kind === 'index') {
        const indexed = await api.indexKnowledge(project.id, project.version);
        if (!current()) return;
        const value = await api.knowledge(project.id);
        if (current()) { setKnowledge(value); setResult(null); setNotice(`已建立 ${indexed.chunks} 个知识片段；新任务会冻结该索引版本。`); }
      } else if (kind === 'search' && knowledge?.usable_generation) {
        const value = await api.searchKnowledge(project.id, knowledge.usable_generation, query.trim(), project.retrieval_mode || 'hybrid');
        if (current()) setResult(value);
      } else if (kind === 'history') {
        await api.sampleProject(project.id);
        if (!current()) return;
        const end = new Date(); const start = new Date(end.getTime() - 30 * 60 * 1000);
        const value = await api.observations(project.id, project.version, start.toISOString(), end.toISOString());
        if (current()) setHistory(value);
      } else if (kind === 'probe') {
        const value = await api.businessProbe(project.id);
        if (current()) setProbe(value);
      }
    } catch (failure) { if (current()) setError(failureText(failure)); }
    finally { if (current()) { action.current = null; setBusy(''); } }
  }

  const generation = knowledge?.generations.find((item) => item.generation === knowledge.usable_generation);
  const reason = retrievalError(result?.error);
  const errorCode = typeof result?.error === 'object' && result.error ? result.error.code : null;
  return <section className={s.connectionTest} aria-label="知识库与运行历史">
    <h4>已保存项目 v{project.version} · 知识库与运行历史</h4>
    <p className={s.muted}>索引只读取你登记的 Markdown / 文本。向量计算在本机执行；诊断时检索片段会随任务证据发送给已配置的云模型。</p>
    <p>向量模型：{knowledge ? `${knowledge.model.model_id} · ${modelState[knowledge.model.state] || knowledge.model.state}` : '正在读取…'}<br />{generation ? `${generation.documents} 个文档 · ${generation.chunks} 个片段 · 索引 ${generation.generation.slice(0, 12)}` : '当前项目版本尚无可用索引'}</p>
    {knowledge?.model.download_required && <p className={s.inlineInfo}>尚未安装本地模型，检索会明确降级为词法方式。按安装文档下载模型后，重新建立索引。</p>}
    {knowledge?.model.state === 'missing_dependencies' && <p className={s.inlineInfo}>缺少本地向量计算依赖，请按安装文档安装 rag 依赖后再建立索引。</p>}
    {generation?.embedding?.indexed === false && <p className={s.inlineInfo} role="status">当前知识版本没有向量索引；即使本机模型已就绪，也需要重新建立知识索引后才能使用混合或向量检索。</p>}
    <div className={s.buttonRow}><button type="button" className={s.secondaryButton} disabled={!!busy || !project.knowledge_paths?.length} onClick={() => { void run('index'); }}>{busy === 'index' ? '正在本机建立索引…' : '建立 / 更新知识索引'}</button><button type="button" className={s.secondaryButton} disabled={!!busy || project.history_enabled === false} onClick={() => { void run('history'); }}>{busy === 'history' ? '正在采样…' : '采样并查看最近 30 分钟'}</button><button type="button" className={s.secondaryButton} disabled={!!busy || !project.business_probe} onClick={() => { void run('probe'); }}>{busy === 'probe' ? '正在验证业务…' : '执行一次已登记业务探针'}</button></div>
    {busy === 'index' && <p className={s.muted}>关闭设置不会取消正在建立的索引，完成后可重新打开查看。</p>}
    <label>试检索（无 LLM 调用）<input aria-label="知识库试检索" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} /></label><button type="button" className={s.secondaryButton} disabled={!!busy || !knowledge?.usable_generation || !query.trim()} onClick={() => { void run('search'); }}>检索已冻结知识版本</button>
    {error && <div className={s.inlineError} role="alert">{error}</div>}{notice && <div className={s.inlineInfo} role="status">{notice}</div>}
    {result && <div className={s.probeResults}>
      <h4>检索：{result.status} · 实际方式 {result.retrieval?.effective_mode || '未知'}</h4>
      {reason && <p className={s.inlineInfo}>{reason}{errorCode && <small>错误代码：{errorCode}</small>}</p>}
      {result.items.map((item) => <div key={item.id}><strong aria-label="知识片段来源">{sourceLocation(item)}</strong><small>{item.id}</small><pre className={s.evidenceJson}>{String(item.content)}</pre></div>)}
      {!result.items.length && <p>本次没有可返回的知识片段。</p>}
    </div>}
    {history && <details open className={s.advancedSettings}><summary>历史覆盖：{history.status} · {String(history.coverage?.samples_in_window ?? 0)} 个样本</summary><p className={s.muted}>采集开始前、停机期间和超出保留期的数据不能补造。缺口、计数器重置和稀疏采样会限制性能结论。</p><pre className={s.evidenceJson}>{JSON.stringify(history.coverage, null, 2)}</pre>{history.items.map((item) => <pre key={item.id} className={s.evidenceJson}>{String(item.content)}</pre>)}</details>}
    {probe && <details open className={s.advancedSettings}><summary>业务探针：{probe.business_ok === true ? '符合登记断言' : '未通过 / 不可用'}</summary>{probe.items.map((item) => <pre key={item.id} className={s.evidenceJson}>{String(item.content)}</pre>)}</details>}
  </section>;
}
