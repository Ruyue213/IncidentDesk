import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api.ts';
import type { EvidenceSnapshot, Outcome, OutcomeAggregate, OutcomeInput, Reference, Run } from './types.ts';
import s from './App.module.css';

function probeReference(item: EvidenceSnapshot): Reference {
  const content = String(item.content);
  const quote = content.match(/"business_ok"\s*:\s*(true|false)/)?.[0] || content.slice(0, 180);
  return { id: item.id, version: String(item.version), quote };
}

export default function OutcomePanel({ run, references, canVerify }: { run: Run; references: Reference[]; canVerify: boolean }) {
  const [saved, setSaved] = useState<Outcome | null>(null);
  const [diagnosis, setDiagnosis] = useState<OutcomeInput['diagnosis_assessment'] | ''>('');
  const [recovery, setRecovery] = useState<OutcomeInput['recovery_assessment'] | ''>('');
  const [diagnosisReason, setDiagnosisReason] = useState('');
  const [recoveryReason, setRecoveryReason] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [aggregate, setAggregate] = useState<OutcomeAggregate | null>(null);
  const [checks, setChecks] = useState<EvidenceSnapshot[]>([]);
  const epoch = useRef(0);
  const key = (r: Reference) => `${r.id}:${r.version}:${r.quote}`;
  const available = Array.from(new Map(
    [...references, ...(saved?.verification_references || []), ...checks.map(probeReference)]
      .map((reference) => [key(reference), reference]),
  ).values());

  useEffect(() => {
    const token = ++epoch.current;
    setLoading(true); setError(''); setNotice(''); setAggregate(null);
    void api.outcome(run.id).then(({ outcome }) => {
      if (epoch.current !== token) return;
      setSaved(outcome); setDiagnosis(outcome?.diagnosis_assessment || '');
      setRecovery(outcome?.recovery_assessment || '');
      setDiagnosisReason(outcome?.diagnosis_reason || ''); setRecoveryReason(outcome?.recovery_reason || '');
      setSelected(outcome?.verification_references.map(key) || []);
    }).catch((failure) => { if (epoch.current === token) setError(failure instanceof Error ? failure.message : '反馈读取失败，请重新打开任务。'); })
      .finally(() => { if (epoch.current === token) setLoading(false); });
    return () => { if (epoch.current === token) epoch.current++; };
  }, [run.id]);

  useEffect(() => {
    if (run.request.mode !== 'live') return;
    let active = true;
    void api.businessVerifications(run.id).then(({ items }) => {
      if (active) setChecks(items);
    }).catch((failure) => {
      if (active) setError(failure instanceof Error ? failure.message : '业务核对记录读取失败');
    });
    return () => { active = false; };
  }, [run.id, run.request.mode]);

  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy || loading || !diagnosis || !recovery) return;
    const token = epoch.current; setBusy(true); setError(''); setNotice('');
    const verification = available.filter((reference) => selected.includes(key(reference)));
    if ((diagnosis !== 'inconclusive' || recovery !== 'not_attempted') && !verification.length) {
      setError('请勾选支持本次核对结论的证据引用；没有充分证据时请选择“尚无法确认”和“未尝试恢复”。'); setBusy(false); return;
    }
    try {
      const { outcome } = await api.saveOutcome(run.id, {
        diagnosis_assessment: diagnosis, diagnosis_reason: diagnosisReason,
        recovery_assessment: recovery, recovery_reason: recoveryReason,
        verification_references: verification, expected_revision: saved?.revision || 0,
      });
      if (epoch.current === token) { setSaved(outcome); setNotice(`已保存人工核对 v${outcome.revision}，不会改变任务和执行回执。`); setAggregate(null); }
    } catch (failure) { if (epoch.current === token) setError(failure instanceof Error ? failure.message : '反馈保存失败'); }
    finally { if (epoch.current === token) setBusy(false); }
  }

  async function verify() {
    if (busy) return;
    const token = epoch.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const batch = await api.verifyBusiness(run.id);
      if (epoch.current !== token) return;
      setChecks((previous) => [...batch.items, ...previous].slice(0, 20));
      setNotice(batch.business_ok === true
        ? '登记的业务断言通过。请核对证据后填写结果。'
        : '业务断言未通过或探针不可用，已保留本次证据。');
    } catch (failure) {
      if (epoch.current === token) setError(failure instanceof Error ? failure.message : '业务核对失败');
    } finally { if (epoch.current === token) setBusy(false); }
  }

  async function summary() {
    const token = epoch.current; setBusy(true); setError('');
    try { const result = await api.outcomeAggregate(); if (epoch.current === token) setAggregate(result); }
    catch (failure) { if (epoch.current === token) setError(failure instanceof Error ? failure.message : '汇总读取失败'); }
    finally { if (epoch.current === token) setBusy(false); }
  }

  return <section className={s.usageCard} aria-label="人工核对与业务结果">
    <h3>人工核对与业务结果</h3>
    <p className={s.muted}>任务完成只表示执行流程结束。请依据实际检查记录诊断是否成立、业务是否恢复；反馈由本机操作者填写，不作为独立验证的准确率。</p>
    {canVerify && <button className={s.secondaryButton} type="button" disabled={loading || busy} onClick={() => { void verify(); }}>运行只读业务检查并保存证据</button>}
    {checks.length > 0 && <details className={s.advancedSettings}><summary>已保存的业务检查（{checks.length}）</summary>{checks.map((item) => <pre className={s.evidenceJson} key={item.id}>{String(item.content)}</pre>)}</details>}
    <details className={s.advancedSettings}><summary>{saved ? `已记录人工核对 v${saved.revision} · 查看或修订` : '尚未记录 · 填写本次核对'}</summary>
      <form className={s.form} onSubmit={(event) => { void submit(event); }}>
        <label>诊断核对<select aria-label="诊断核对" required value={diagnosis} disabled={loading || busy} onChange={(event) => setDiagnosis(event.target.value as typeof diagnosis)}><option value="">请明确选择</option><option value="confirmed" disabled={!run.diagnosis}>有证据支持诊断</option><option value="rejected" disabled={!run.diagnosis}>有证据反驳诊断</option><option value="inconclusive">尚无法确认</option></select></label>
        <label>诊断核对依据<textarea aria-label="诊断核对依据" required minLength={3} maxLength={2000} value={diagnosisReason} onChange={(event) => setDiagnosisReason(event.target.value)} /></label>
        <label>业务恢复<select aria-label="业务恢复" required value={recovery} disabled={loading || busy} onChange={(event) => setRecovery(event.target.value as typeof recovery)}><option value="">请明确选择</option><option value="verified">已通过运行证据核对恢复</option><option value="failed">已尝试但尚未恢复</option><option value="not_attempted">未尝试恢复</option></select></label>
        <label>业务核对依据<textarea aria-label="业务核对依据" required minLength={3} maxLength={2000} value={recoveryReason} onChange={(event) => setRecoveryReason(event.target.value)} /></label>
        <fieldset><legend>核对引用（恢复结论至少选择一项运行证据）</legend>{available.length ? available.map((reference) => <label className={s.checkboxCard} key={key(reference)}><input type="checkbox" checked={selected.includes(key(reference))} onChange={(event) => setSelected((previous) => event.target.checked ? [...previous, key(reference)] : previous.filter((value) => value !== key(reference)))} /><span>{reference.quote}<small>{reference.id} · v{reference.version}</small></span></label>) : <p className={s.muted}>当前没有可选引用。先执行业务检查，或记录“尚无法确认／未尝试恢复”。</p>}</fieldset>
        <button className={s.primaryButton} type="submit" disabled={busy || loading}>{busy ? '正在处理…' : '保存人工核对'}</button>
      </form>
    </details>
    <button className={s.textButton} type="button" disabled={busy} onClick={() => { void summary(); }}>查看真实项目的本机汇总</button>
    {aggregate && <details open className={s.advancedSettings}><summary>本机人工核对汇总</summary>
      <p>已核对 {aggregate.annotated_runs} / {aggregate.terminal_runs} 个结束任务。</p>
      <p>诊断：支持 {aggregate.diagnosis_assessments.confirmed} · 反驳 {aggregate.diagnosis_assessments.rejected} · 待确认 {aggregate.diagnosis_assessments.inconclusive}</p>
      <p>业务：{aggregate.recovery_assessments.verified} / {aggregate.attempted_recovery_annotations} 次尝试记录为已恢复 · {aggregate.recovery_assessments.not_attempted} 次未尝试。</p>
      <a className={s.textButton} download="incidentdesk-outcome-aggregate.json" href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(aggregate, null, 2))}`}>下载仅含统计的记录</a>
    </details>}
    {error && <p className={s.inlineError} role="alert">{error}</p>}{notice && <p className={s.inlineInfo} role="status">{notice}</p>}
  </section>;
}
