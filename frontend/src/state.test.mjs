import assert from 'node:assert/strict';
import test from 'node:test';
import {
  approvalPayload, composeQuestion, EVENT_KINDS, LEGACY_TICKET_SUFFIX, mergeEvents, preferRun, readEventCache,
  restoreQuestion, submissionIntent, writeStore,
} from './state.ts';

const input = {
  question: '检查发布后的异常', service_id: 'checkout-api', incident_id: 'dev-001',
  start: '2026-10-01T08:00:00.000Z', end: '2026-10-01T09:00:00.000Z',
};
const event = (id, run = 'one') => ({
  event_id: id, id, run_id: run, kind: 'run.state_changed', payload: {}, created_at: 'now',
});
const memory = () => {
  const data = new Map();
  return { getItem: (key) => data.get(key) || null, setItem: (key, value) => data.set(key, value), removeItem: (key) => data.delete(key) };
};

test('report validation feedback is subscribed to as a durable event that refreshes run state', () => {
  assert.equal(EVENT_KINDS.includes('report.validation_failed'), true);
  const feedback = { ...event(3), kind: 'report.validation_failed', payload: { attempt: 1, error: 'not rendered as event label' } };
  const cache = mergeEvents({ cursor: 2, events: [event(2)] }, [feedback], 'one');
  assert.equal(cache.cursor, 3);
  assert.equal(cache.events.at(-1).payload.attempt, 1);
});

test('reconnect backfill deduplicates by ID and never mixes two run histories', () => {
  const result = mergeEvents({ cursor: 2, events: [event(1), event(2)] }, [event(2), event(4), event(3), event(99, 'other')], 'one');
  assert.deepEqual(result.events.map((item) => item.event_id), [1, 2, 3, 4]);
  assert.equal(result.cursor, 4);
  const bounded = mergeEvents(result, Array.from({ length: 300 }, (_, n) => event(n + 5)), 'one');
  assert.equal(bounded.events.length, 250);
  assert.equal(bounded.events.at(-1).event_id, bounded.cursor);
});

test('cursor without matching persisted history cannot skip events after refresh', () => {
  const store = memory();
  writeStore(store, 'incidentdesk.events.one', { cursor: 50, events: [] });
  assert.deepEqual(readEventCache(store, 'one'), { cursor: 0, events: [] });
  const complete = { cursor: 4, events: [event(3), event(4)] };
  writeStore(store, 'incidentdesk.events.one', complete);
  assert.deepEqual(readEventCache(store, 'one'), complete);
});

test('unknown submit response preserves the original body and idempotency key', () => {
  const first = submissionIntent(input, null, () => 'original-key');
  const repeated = submissionIntent({ ...input }, first, () => 'must-not-be-used');
  assert.equal(repeated, first);
  const changed = submissionIntent({ ...input, question: '这是一个新的诊断问题' }, first, () => 'new-key');
  assert.equal(changed.key, 'new-key');
});

test('refreshing an unconfirmed ticket submission restores form intent without duplicating suffix', () => {
  const body = { ...input, question: composeQuestion(input.question, true) };
  const first = submissionIntent(body, null, () => 'original-key');
  const form = restoreQuestion(first.body.question);
  assert.equal(form.ticket, true);
  const retriedBody = { ...input, question: composeQuestion(form.question, form.ticket) };
  assert.deepEqual(retriedBody, body);
  assert.equal(submissionIntent(retriedBody, first, () => 'wrong-new-key').key, 'original-key');
  assert.equal(composeQuestion(body.question, true), body.question);
});

test('approval is bound to the captured frozen operation, without editable draft parameters', () => {
  const operation = { id: 'op', interrupt_id: 'interrupt', draft_version: 1, params_digest: 'digest', params: { draft: {} } };
  const payload = approvalPayload(operation, 'approve');
  operation.id = 'new-op'; operation.draft_version = 2; operation.params_digest = 'changed';
  assert.deepEqual(payload, { operation_id: 'op', interrupt_id: 'interrupt', draft_version: 1, params_digest: 'digest', decision: 'approve' });
  assert.equal('params' in payload, false);
});

test('a legacy restored ticket request retains exact original body/key after UI wording upgrade', () => {
  const original = { ...input, question: input.question + LEGACY_TICKET_SUFFIX };
  const previous = submissionIntent(original, null, () => 'original-key');
  const form = restoreQuestion(previous.body.question);
  const candidate = { ...input, question: composeQuestion(form.question, form.ticket) };
  assert.notEqual(candidate.question, original.question);
  const retry = submissionIntent(candidate, previous, () => 'must-not-create-a-new-run');
  assert.equal(retry.key, 'original-key');
  assert.deepEqual(retry.body, original);
  assert.equal(composeQuestion(original.question, true), original.question);
});

test('restored timestamp formatting cannot silently create a different submission', () => {
  const previous = submissionIntent({ ...input, start: '2026-10-01T16:00:00+08:00' }, null, () => 'original-key');
  const retry = submissionIntent(input, previous, () => 'must-not-create-a-new-run');
  assert.equal(retry, previous);
  assert.equal(retry.body.start, '2026-10-01T16:00:00+08:00');
  assert.equal(submissionIntent({ ...input, end: '2026-10-01T10:00:00Z' }, previous, () => 'new-key').key, 'new-key');
});

test('live submit retries bind project, model and follow-up context to the original intent', () => {
  const live = { ...input, mode: 'live', incident_id: null, service_id: 'socialengine', project_version: 2, model_profile_version: 3, parent_run_id: 'parent-a' };
  const previous = submissionIntent(live, null, () => 'original-key');
  assert.equal(submissionIntent({ ...live }, previous, () => 'wrong-key'), previous);
  for (const changed of [{ project_version: 4 }, { model_profile_version: 4 }, { parent_run_id: 'parent-b' }, { service_id: 'another-project' }, { mode: 'fixture', incident_id: 'dev-001' }]) {
    assert.equal(submissionIntent({ ...live, ...changed }, previous, () => 'new-key').key, 'new-key');
  }
});

test('legacy fixture intent and explicit fixture mode retain the same exact original body', () => {
  const previous = submissionIntent(input, null, () => 'original-key');
  const retry = submissionIntent({ ...input, mode: 'fixture', project_version: null, model_profile_version: null, parent_run_id: null }, previous, () => 'wrong-key');
  assert.equal(retry, previous);
  assert.equal('mode' in retry.body, false);
});

test('a stale list poll cannot replace a later terminal or approved task view', () => {
  const base = { id: 'run', status: 'RUNNING', revision: 2, updated_at: '2026-01-01', ticket_id: null, cancel_requested: false, operation: null, usage: { model_calls: 2, unknown_usage_calls: 0 }, active_seconds: 1, tool_calls: 1 };
  const done = { ...base, status: 'SUCCEEDED', revision: 4, ticket_id: 'ticket', operation: { status: 'COMMITTED' } };
  assert.equal(preferRun(done, base), done);
  const approved = { ...base, operation: { status: 'APPROVED' } };
  assert.equal(preferRun(approved, { ...base, operation: { status: 'PENDING' } }), approved);
  assert.equal(preferRun(base, { ...base, usage: { model_calls: 1 } }), base);
  const executed = { ...done, action_receipt: { status: 'SUCCEEDED' } };
  assert.equal(preferRun(executed, { ...done, action_receipt: null }), executed);
});
