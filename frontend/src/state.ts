import type { ApprovalPayload, BusinessEvent, Operation, Run, RunInput, RunStatus } from './types.ts';

export const EVENT_KINDS = [
  'run.queued', 'run.state_changed', 'run.cancel_requested',
  'operation.frozen', 'operation.decided', 'ticket.created',
  'action.started', 'action.finished', 'tool.started', 'tool.finished',
  'report.validation_failed',
] as const;
export const isTerminal = (status: RunStatus) => ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(status);
export interface EventCache { cursor: number; events: BusinessEvent[] }
export interface SubmissionIntent { key: string; body: RunInput }
export const TICKET_SUFFIX = '\n诊断完成后，请发起创建人工交接工单的申请，等待人工批准；不要只返回建议草案。';
export const LEGACY_TICKET_SUFFIX = '\n请提出人工交接工单，等待人工批准后再创建。';
const TICKET_SUFFIXES = [TICKET_SUFFIX, LEGACY_TICKET_SUFFIX];
export function composeQuestion(question: string, ticket: boolean): string {
  const text = question.trim();
  return ticket && !TICKET_SUFFIXES.some((suffix) => text.endsWith(suffix.trim()))
    ? text + TICKET_SUFFIX : text;
}
export function restoreQuestion(question: string) {
  const suffix = TICKET_SUFFIXES.find((value) => question.endsWith(value));
  return suffix
    ? { question: question.slice(0, -suffix.length), ticket: true }
    : { question, ticket: false };
}
type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function readStore<T>(store: Store, key: string): T | null {
  try { return JSON.parse(store.getItem(key) || 'null') as T | null; } catch { return null; }
}
export function writeStore(store: Store, key: string, value: unknown) {
  try { store.setItem(key, JSON.stringify(value)); } catch { /* Memory state remains usable. */ }
}
export function removeStore(store: Store, key: string) {
  try { store.removeItem(key); } catch { /* Private browsing can disable persistence. */ }
}
export function mergeEvents(current: EventCache, incoming: BusinessEvent[], runId: string): EventCache {
  const byId = new Map<number, BusinessEvent>();
  for (const event of [...current.events, ...incoming]) {
    if (event.run_id === runId && Number.isSafeInteger(event.event_id) && event.event_id > 0) {
      byId.set(event.event_id, event);
    }
  }
  const events = [...byId.values()].sort((a, b) => a.event_id - b.event_id).slice(-250);
  return { events, cursor: Math.max(current.cursor, events.at(-1)?.event_id || 0) };
}
export function readEventCache(store: Store, runId: string): EventCache {
  const cached = readStore<EventCache>(store, `incidentdesk.events.${runId}`);
  if (!cached || !Array.isArray(cached.events) || !Number.isSafeInteger(cached.cursor)) {
    return { events: [], cursor: 0 };
  }
  const result = mergeEvents({ events: [], cursor: 0 }, cached.events, runId);
  // Cursor and rendered events are one persisted record. A cursor without its
  // corresponding event history must not skip history after a page refresh.
  return result.cursor === cached.cursor ? result : { events: [], cursor: 0 };
}
function bodySignature(body: RunInput): string {
  const question = restoreQuestion(body.question.trim());
  const instant = (value: string) => {
    const parsed = new Date(value);
    return Number.isFinite(parsed.valueOf()) ? parsed.toISOString() : value;
  };
  // Recognize the same restored UI intent across wording/time-format upgrades,
  // then return the original body below. Never send changed bytes under an old key.
  return JSON.stringify([
    question.question, question.ticket, body.mode || 'fixture', body.service_id, body.incident_id || null,
    instant(body.start), instant(body.end),
    body.project_version ?? null, body.model_profile_version ?? null, body.parent_run_id ?? null,
  ]);
}
export function submissionIntent(
  body: RunInput, previous: SubmissionIntent | null, newKey: () => string,
): SubmissionIntent {
  return previous?.key && previous.body && bodySignature(previous.body) === bodySignature(body)
    ? previous : { key: newKey(), body: { ...body } };
}
export function approvalPayload(operation: Operation, decision: 'approve' | 'reject'): ApprovalPayload {
  return {
    interrupt_id: operation.interrupt_id, operation_id: operation.id,
    draft_version: operation.draft_version, params_digest: operation.params_digest, decision,
  };
}
export function preferRun(previous: Run | undefined, incoming: Run): Run {
  if (!previous || previous.id !== incoming.id) return incoming;
  if (isTerminal(previous.status) && !isTerminal(incoming.status)) return previous;
  if (incoming.revision < previous.revision) return previous;
  if (incoming.revision > previous.revision) return incoming;
  if (incoming.updated_at < previous.updated_at) return previous;
  if (previous.ticket_id && !incoming.ticket_id) return previous;
  if (previous.action_receipt && !incoming.action_receipt) return previous;
  if (previous.cancel_requested && !incoming.cancel_requested) return previous;
  const operationRank = { PENDING: 0, APPROVED: 1, REJECTED: 1, COMMITTED: 2 };
  if (previous.operation && (!incoming.operation || operationRank[incoming.operation.status] < operationRank[previous.operation.status])) return previous;
  if (incoming.usage.model_calls < previous.usage.model_calls || incoming.tool_calls < previous.tool_calls || incoming.active_seconds < previous.active_seconds) return previous;
  if (incoming.usage.model_calls === previous.usage.model_calls && incoming.usage.unknown_usage_calls > previous.usage.unknown_usage_calls) return previous;
  return incoming;
}
