import { useRef, useState } from 'react';
import { api } from './api.ts';
import type { Run } from './types.ts';
import s from './App.module.css';

export default function RunAuditPanel({ run }: { run: Run }) {
  const [retrieval, setRetrieval] = useState<Record<string, unknown>[] | null>(null);
  const [cloud, setCloud] = useState<Record<string, unknown>[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const action = useRef(false);
  async function load() {
    if (action.current) return;
    action.current = true; setBusy(true); setError('');
    try {
      const results = await Promise.all([api.retrievalTrace(run.id), api.cloudAudit(run.id)]);
      setRetrieval(results[0]); setCloud(results[1].items);
    } catch (failure) { setError(failure instanceof Error ? failure.message : '执行记录未能读取'); }
    finally { action.current = false; setBusy(false); }
  }
  return <section className={s.usageCard} aria-label="检索与发送记录">
    <details className={s.advancedSettings}><summary>本任务冻结的检索与数据规则</summary><pre className={s.evidenceJson}>{JSON.stringify({ retrieval: run.request.retrieval_config || '旧任务 · 保留原检索策略', mode: run.request.retrieval_mode, generation: run.request.knowledge_generation, cloud_policy: run.request.cloud_policy ? { ...run.request.cloud_policy, redact_literals: `${run.request.cloud_policy.redact_literals.length} 项（此处不展示字词）` } : '旧任务 · 使用原项目规则' }, null, 2)}</pre>
      <button className={s.secondaryButton} type="button" disabled={busy} onClick={() => { void load(); }}>{busy ? '正在读取…' : '读取本机执行记录'}</button>
      {retrieval && <><h4>检索阶段与排序变化</h4><p className={s.muted}>包含候选来源、分数和耗时；不包含未选中片段的正文。</p><pre className={s.evidenceJson}>{JSON.stringify(retrieval, null, 2)}</pre></>}
      {cloud && <><h4>云端发送策略检查</h4><p className={s.muted}>仅记录策略决定、发送体摘要与字节数。允许记录不代表供应商已经接收请求。</p><pre className={s.evidenceJson}>{JSON.stringify(cloud, null, 2)}</pre></>}
      {error && <p className={s.inlineError} role="alert">{error}</p>}
    </details>
  </section>;
}
