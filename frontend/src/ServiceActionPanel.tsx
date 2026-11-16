import type { ReactNode } from 'react';
import type { Reference, Run } from './types.ts';
import Icon from './Icon.tsx';
import s from './App.module.css';

export default function ServiceActionPanel({ run, pending, busy, referenceButton, onDecision }: {
  run: Run; pending: boolean; busy: string | null; referenceButton: (reference: Reference, index: number) => ReactNode;
  onDecision: (decision: 'approve' | 'reject') => void;
}) {
  const operation = run.operation;
  if (!operation || operation.kind !== 'service_action') return null;
  const request = operation.params.request;
  const preview = operation.params.preview;
  const receipt = run.action_receipt;
  const result = receipt?.result;
  const receiptLabel = { STARTED: '动作已开始 · 等待回执', SUCCEEDED: '动作已完成', FAILED: '动作执行失败', UNKNOWN: '动作结果未知 · 需人工核对' };
  const label = receipt ? receiptLabel[receipt.status] : operation.decision === 'reject' ? '已拒绝执行' : operation.decision === 'approve' ? '已批准 · 等待实际回执' : '等待人工批准';
  return <section className={s.ticketCard} data-pending={pending} aria-labelledby="action-title">
    <div className={s.cardHeading}><div><span className={s.sectionNumber}>02</span><h3 id="action-title">服务操作</h3></div><span className={s.operationLabel} data-status={operation.status}>{label}</span></div>
    <div className={s.draftBody}><div className={s.draftTitle}><h4>{preview?.title || (request?.action === 'restart_service' ? '重启本机服务' : '更新服务配置')}</h4><span className={s.priority}>人工批准后执行</span></div>
      <p>{request?.reason}</p><dl className={s.actionFacts}><dt>目标</dt><dd>{preview?.target || request?.path || run.request.service_id}</dd><dt>冻结版本</dt><dd>v{operation.draft_version}</dd></dl>
      {preview?.command && <><h4>将执行的程序与参数</h4><pre className={s.actionCode} aria-label="启动命令预览">{JSON.stringify(preview.command, null, 2)}</pre></>}
      {preview?.diff !== undefined && <><h4>配置变更预览</h4><pre className={s.actionCode} aria-label="配置变更差异">{preview.diff || '无文本差异'}</pre></>}
      {preview?.before_sha256 && <p className={s.hashLine}>执行前文件 SHA-256：{preview.before_sha256}</p>}
      {!!preview?.warnings.length && <div className={s.inlineInfo}><strong>执行前核对</strong><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
      <div className={s.draftReferences}><span>冻结操作引用的证据</span><div>{(request?.references || []).map(referenceButton)}</div></div>
    </div>
    {receipt ? <div className={s.actionReceipt} data-status={receipt.status}><strong>{receiptLabel[receipt.status]}</strong>{result?.message && <p>{result.message}</p>}
      {result?.error_type && <p>错误类型：{result.error_type}</p>}
      {receipt.status === 'UNKNOWN' && <p>后台不会自动重放结果不明的外部动作。请先核对服务进程与文件状态。</p>}
      <dl className={s.actionFacts}>{result?.pid !== undefined && <><dt>进程 PID</dt><dd>{result.pid}</dd></>}{result?.path && <><dt>配置文件</dt><dd>{result.path}</dd></>}{result?.backup_id && <><dt>备份编号</dt><dd>{result.backup_id}</dd></>}{result?.before_sha256 && <><dt>修改前 SHA-256</dt><dd>{result.before_sha256}</dd></>}{result?.after_sha256 && <><dt>修改后 SHA-256</dt><dd>{result.after_sha256}</dd></>}{result?.health_before && <><dt>执行前健康检查</dt><dd>{result.health_before.status}{result.health_before.http_status ? ` · HTTP ${result.health_before.http_status}` : ''}</dd></>}{result?.health_after && <><dt>执行后健康检查</dt><dd>{result.health_after.status}{result.health_after.http_status ? ` · HTTP ${result.health_after.http_status}` : ''}</dd></>}</dl>
      {result?.listener_verified !== undefined && <p>监听端口与新进程归属：{result.listener_verified ? '已核实' : '未核实'}</p>}
      <p className={s.recoveryNote}>{result?.recovery_verified === true ? '进程与健康端点核查已通过；业务恢复仍需结合实际请求与指标确认。' : '尚未证实业务恢复。动作执行完成与故障消除是两项独立结果，请继续核对证据。'}</p>
      <small className={s.mono}>操作 {receipt.operation_id}</small>
    </div> : pending ? <div className={s.approvalFooter}><p><Icon name="info" size={15} />只授权当前冻结预览；批准前不会执行重启或配置写入。</p><div><button type="button" className={s.secondaryButton} aria-label="拒绝当前服务操作" disabled={!!busy} onClick={() => onDecision('reject')}>{busy === 'reject' ? '记录中…' : '拒绝执行'}</button><button type="button" className={s.primaryButton} aria-label="批准当前服务操作" disabled={!!busy || !preview || !request} onClick={() => onDecision('approve')}>{busy === 'approve' ? '记录中…' : '批准并执行此操作'}</button></div></div>
      : <div className={s.ticketFootnote}>{operation.decision === 'reject' ? '操作已拒绝，不执行本次服务变更。' : operation.decision === 'approve' ? '批准已记录；请等待实际执行回执，不据此判断服务已恢复。' : '本任务当前不接受服务操作批准。'}</div>}
  </section>;
}
