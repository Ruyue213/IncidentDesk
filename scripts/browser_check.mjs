/** Real Chrome acceptance against an isolated local API and network-blocked scripted worker.
 * Run after `cd frontend && npm ci && npm run build`:
 * node scripts/browser_check.mjs [--record-demo] [--port 8788]
 * No real API key is loaded, and no paid model request is made.
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PYTHON = path.join(ROOT, '.venv/bin/python');
const args = process.argv.slice(2);
const usage = `Usage: node scripts/browser_check.mjs [--record-demo] [--port 8788]\n\nRuns an isolated real-Chrome UI acceptance suite with a network-blocked scripted\nmodel, temporary SQLite databases, and no .env or paid API calls.\n--record-demo records a silent 3–5 minute workflow demonstration.\n--port selects an unused loopback port (default 8788).\n--help, -h prints this text without creating files, launching a browser or server.\n`;
let recordDemo = false, port = 8788, help = false;
for (let index = 0; index < args.length; index += 1) {
  const option = args[index];
  if (option === '--help' || option === '-h') help = true;
  else if (option === '--record-demo') recordDemo = true;
  else if (option === '--port' && /^\d+$/.test(args[index + 1] || '')) port = Number(args[++index]);
  else {
    console.error('Unsupported or incomplete option. Use --help for usage.');
    process.exit(2);
  }
}
if (!Number.isInteger(port) || port <= 1024 || port > 65535) {
  console.error('--port must be an integer between 1025 and 65535.');
  process.exit(2);
}
if (help) { console.log(usage); process.exit(0); }
// Parsing and help must finish before optional dependencies or any side effects.
const require = createRequire(path.join(ROOT, 'frontend/package.json'));
const { chromium } = require('playwright');
const ORIGIN = `http://127.0.0.1:${port}`;
const stamp = new Date().toISOString().replaceAll(/[:.]/g, '-');
const OUT = path.join(ROOT, 'docs/evidence/CP11', `browser-${stamp}`);
const localDir = await mkdtemp(path.join(os.tmpdir(), 'incident-desk-browser-'));
await mkdir(OUT, { recursive: true });
const environment = { ...process.env, LANGSMITH_TRACING: 'false', LANGCHAIN_TRACING_V2: 'false' };
for (const name of ['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'LANGSMITH_API_KEY']) delete environment[name];
const report = {
  started_at: new Date().toISOString(), status: 'RUNNING', origin: ORIGIN,
  scope: 'Actual Chrome UI + actual temporary SQLite API + network-blocked scripted worker; no real model quality claim',
  model_api_calls: 0, frontend_index: await readFile(path.join(ROOT, 'frontend/dist/index.html'), 'utf8'),
  steps: [], worker_processes: [], run_ids: [], page_errors: [], console_errors: [], expected_network_failures: [],
};
let browser, context, page, server, video;
let expectedNetwork = false;
const workerCode = `
import asyncio, socket, sys
from pathlib import Path
from incident_desk.settings import Settings
from incident_desk.agent.reliability_model import make_reliability_model
from incident_desk.worker.runtime import Worker
def forbid(*args, **kwargs): raise RuntimeError('network forbidden in offline browser acceptance worker')
socket.socket.connect=forbid
socket.create_connection=forbid
settings=Settings(_env_file=None, api_key=None, local_dir=Path(sys.argv[1]))
asyncio.run(Worker(settings,model_factory=make_reliability_model,crash_at=sys.argv[2] or None).run(once=True))
`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitUntil(fn, description, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return;
    await sleep(100);
  }
  throw new Error(`Timed out: ${description}`);
}
async function json(url, init) {
  const response = await fetch(ORIGIN + url, init);
  const value = await response.json();
  assert(response.ok, `${url}: ${response.status} ${JSON.stringify(value)}`);
  return value;
}
const getRun = (id) => json(`/api/runs/${id}`);
async function python(code, values = [], expected = 0) {
  return await new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ['-c', code, ...values], { cwd: ROOT, env: environment });
    let stdout = '', stderr = '';
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('Python child timeout')); }, 30000);
    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== expected) reject(new Error(`Child exited ${code}, expected ${expected}: ${stderr}`));
      else resolve({ exit_code: code, stdout, stderr });
    });
  });
}
async function worker(crash = '') {
  const result = await python(workerCode, [localDir, crash], crash ? 86 : 0);
  report.worker_processes.push({ crash_at: crash || null, ...result });
  return result;
}
async function step(name, action, { hold = true } = {}) {
  const item = { name, started_at: new Date().toISOString(), status: 'RUNNING' };
  report.steps.push(item);
  await writeFile(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  try {
    item.result = await action(); item.status = 'PASSED';
    if (recordDemo && hold) await sleep(5000);
  } catch (error) {
    item.status = 'FAILED'; item.error = { type: error.name, message: error.message };
    throw error;
  } finally {
    item.finished_at = new Date().toISOString();
    await writeFile(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ step: name, status: item.status }));
  }
}
async function screenshot(name) {
  await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true });
}
async function selected(id) {
  await waitUntil(() => page.evaluate((given) => localStorage.getItem('incidentdesk.selectedRun') === JSON.stringify(given), id), 'selected run persisted');
  await waitUntil(async () => (await page.locator('section[aria-label="任务诊断详情"]').innerText()).includes('#' + id.slice(0, 8)), 'selected detail matches run');
}
async function select(id) {
  await page.getByRole('button', { name: new RegExp(`选择任务 ${id.slice(0, 8)}`) }).click();
  await selected(id);
}
async function showForm(label, incident = 'dev-001') {
  await page.getByRole('button', { name: '新建诊断任务', exact: true }).click();
  await page.getByLabel('任务来源').selectOption('fixture');
  await page.getByLabel('选择模拟案例').selectOption(incident);
  await page.getByLabel('异常描述').fill(`【离线脚本模型验收：${label}】请依据模拟证据诊断；此任务只验证浏览器和事务恢复。`);
  await page.getByLabel('申请人工交接工单').check();
}
async function submit(label, incident) {
  await showForm(label, incident);
  const received = page.waitForResponse((r) => r.url() === ORIGIN + '/api/runs' && r.request().method() === 'POST');
  await page.getByRole('button', { name: '提交诊断任务', exact: true }).click();
  const response = await received;
  assert.equal(response.status(), 202);
  const run = await response.json(); report.run_ids.push(run.id);
  await selected(run.id);
  return run;
}
async function waitApproval(id) {
  await waitUntil(async () => (await getRun(id)).status === 'WAITING_APPROVAL', 'durable approval interrupt');
  await page.getByRole('button', { name: '批准当前工单草案' }).waitFor();
  const run = await getRun(id);
  assert.equal(run.ticket_id, null); assert.equal(run.operation.decision, null);
  return run;
}
async function terminal(id, expected) {
  await waitUntil(async () => (await getRun(id)).status === expected, 'terminal ' + expected);
  await waitUntil(async () => (await page.locator('section[aria-label="任务诊断详情"]').innerText()).includes('终态已同步'), 'terminal event backlog drained');
  const run = await getRun(id);
  const events = (await json(`/api/runs/${id}/events?after=0&limit=200`)).items;
  const cache = await page.evaluate((key) => JSON.parse(localStorage.getItem(key) || 'null'), `incidentdesk.events.${id}`);
  assert(cache && cache.cursor === events.at(-1).event_id);
  assert.equal(new Set(cache.events.map((e) => e.event_id)).size, cache.events.length);
  assert(cache.events.every((e) => e.run_id === id));
  const sources = await page.evaluate(() => window.__qaSSE);
  assert(sources.filter((s) => s.url.includes(id)).every((s) => s.closed), 'terminal stream must close');
  return { run, cache, events };
}

try {
  await new Promise((resolve, reject) => {
    const probe = net.createServer(); probe.once('error', reject);
    probe.listen(port, '127.0.0.1', () => probe.close(resolve));
  });
  const log = createWriteStream(path.join(OUT, 'api-server.txt'));
  const apiCode = `from pathlib import Path; import sys,uvicorn; from incident_desk.api.app import create_app; from incident_desk.settings import Settings; uvicorn.run(create_app(Settings(_env_file=None,api_key=None,local_dir=Path(sys.argv[1]))),host='127.0.0.1',port=int(sys.argv[2]),log_level='warning')`;
  server = spawn(PYTHON, ['-c', apiCode, localDir, String(port)], { cwd: ROOT, env: environment });
  server.stdout.pipe(log); server.stderr.pipe(log);
  await waitUntil(async () => { try { return (await fetch(ORIGIN + '/health')).ok; } catch { return false; } }, 'isolated API ready');
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  context = await browser.newContext({ viewport: { width: 1520, height: 1020 },
    ...(recordDemo ? { recordVideo: { dir: path.join(OUT, 'video'), size: { width: 1520, height: 1020 } } } : {}) });
  await context.addInitScript(() => {
    window.__qaSSE = [];
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url, options) { super(url, options); this.meta = { url: String(url), closed: false }; window.__qaSSE.push(this.meta); }
      close() { this.meta.closed = true; return super.close(); }
    };
  });
  page = await context.newPage(); video = page.video();
  page.setDefaultTimeout(12000);
  page.on('pageerror', (error) => report.page_errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') (expectedNetwork ? report.expected_network_failures : report.console_errors).push({ message: message.text(), location: message.location() });
  });
  report.http_errors = [];
  page.on('response', (response) => { if (response.status() >= 400) report.http_errors.push({url:response.url(),status:response.status(),intentional_fault:expectedNetwork}); });
  page.on('requestfailed', (request) => {
    report.expected_network_failures.push({ url: request.url(), reason: request.failure()?.errorText, intentionally_offline_or_aborted: expectedNetwork });
  });
  await page.goto(ORIGIN, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: '新建诊断任务', exact: true }).waitFor();
  let first, firstWaiting, firstTicket;
  await step('UI submit persists queued task without running model in HTTP', async () => {
    first = await submit('批准与故障恢复');
    assert.equal(first.status, 'QUEUED'); return { run_id: first.id, status: first.status };
  });
  await step('Offline worker exposes real graph interrupt with no ticket', async () => {
    await worker(); firstWaiting = await waitApproval(first.id);
    return { status: firstWaiting.status, interrupt_id: firstWaiting.operation.interrupt_id, ticket_id: null };
  });
  await step('Reference opens actual source snapshot including observed_at', async () => {
    const ref = firstWaiting.operation.params.references[0];
    await page.getByRole('button', { name: `查看证据 ${ref.id}`, exact: true }).first().click();
    await page.getByLabel('证据快照原文').waitFor();
    assert((await page.getByLabel('证据快照原文').innerText()).includes(ref.quote));
    await page.getByText('记录时间', { exact: true }).waitFor();
    await screenshot('waiting-with-source'); return { reference: ref, snapshot: await json(`/api/runs/${first.id}/evidence/${ref.id}`) };
  });
  await step('Refresh restores the same pending operation and event cursor', async () => {
    await page.reload({ waitUntil: 'domcontentloaded' }); await selected(first.id); await waitApproval(first.id);
    assert.equal((await getRun(first.id)).operation.id, firstWaiting.operation.id); return { operation_id: firstWaiting.operation.id };
  });
  await step('Stale interrupt approval receives 409 and UI refresh feedback', async () => {
    const url = ORIGIN + `/api/runs/${first.id}/resume`;
    const intercept = async (route) => route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), interrupt_id: 'stale-browser-interrupt' }) });
    await page.route(url, intercept); expectedNetwork = true;
    const reply = page.waitForResponse((r) => r.url() === url && r.request().method() === 'POST');
    await page.getByRole('button', { name: '批准当前工单草案' }).click();
    assert.equal((await reply).status(), 409);
    await page.getByRole('alert').filter({ hasText: '草案或决定已变化' }).waitFor();
    await page.unroute(url, intercept); expectedNetwork = false;
    assert.equal((await getRun(first.id)).operation.decision, null); return { status: 409, ticket_id: null };
  });
  await step('Double click approves exactly one frozen operation', async () => {
    const url = ORIGIN + `/api/runs/${first.id}/resume`; let posts = 0;
    const count = (r) => { if (r.url() === url && r.method() === 'POST') posts += 1; };
    page.on('request', count);
    await page.getByRole('button', { name: '批准当前工单草案' }).dblclick({ delay: 0 });
    await waitUntil(async () => (await getRun(first.id)).operation.decision === 'approve', 'decision committed');
    await sleep(300); page.off('request', count); assert.equal(posts, 1); return { approval_posts: posts };
  });
  await step('Crash after business commit loses response but preserves one effect', async () => {
    await worker('after_ticket_commit'); const run = await getRun(first.id);
    assert(run.ticket_id); assert.equal(run.status, 'RUNNING'); firstTicket = run.ticket_id;
    await page.getByLabel('工单编号').waitFor(); assert.equal(await page.getByLabel('工单编号').innerText(), firstTicket);
    await screenshot('committed-before-recovery'); return { exit_code: 86, ticket_id: firstTicket, status: run.status };
  });
  await step('Fresh worker resumes and reuses ticket; terminal survives refresh', async () => {
    await worker(); const final = await terminal(first.id, 'SUCCEEDED');
    assert.equal(final.run.ticket_id, firstTicket); assert.equal(final.run.active_started_at, null);
    await page.reload({ waitUntil: 'domcontentloaded' }); await selected(first.id); await terminal(first.id, 'SUCCEEDED');
    await page.getByRole('button', { name: `查看证据 ${firstWaiting.operation.params.references[0].id}`, exact: true }).first().click();
    await page.getByLabel('证据快照原文').waitFor(); await screenshot('desktop-completed');
    return { ticket_id: final.run.ticket_id, events: final.events.length, cursor: final.cache.cursor };
  });
  let rejected;
  await step('Reject creates no ticket while diagnostic result completes', async () => {
    rejected = await submit('拒绝路径'); await worker(); await waitApproval(rejected.id);
    await page.getByRole('button', { name: '拒绝当前工单草案' }).click();
    await waitUntil(async () => (await getRun(rejected.id)).operation.decision === 'reject', 'rejection committed');
    await worker(); const final = await terminal(rejected.id, 'SUCCEEDED');
    assert.equal(final.run.ticket_id, null); assert.equal(final.run.operation.status, 'REJECTED');
    return { run_id: rejected.id, status: final.run.status, ticket_id: null };
  });
  let cancelled;
  await step('Offline browser reconnects and catches cancellation committed meanwhile', async () => {
    cancelled = await submit('断线取消与补读'); await worker(); await waitApproval(cancelled.id);
    expectedNetwork = true; await context.setOffline(true);
    await json(`/api/runs/${cancelled.id}/cancel`, { method: 'POST' }); await worker();
    await sleep(300); await context.setOffline(false);
    const final = await terminal(cancelled.id, 'CANCELLED'); expectedNetwork = false;
    assert.equal(final.run.ticket_id, null); return { run_id: cancelled.id, cursor: final.cache.cursor, status: final.run.status };
  });
  await step('Terminal backlog second read fails once and retries without manual refresh', async () => {
    await page.evaluate((id) => localStorage.removeItem(`incidentdesk.events.${id}`), cancelled.id);
    let requests = 0, failures = 0; expectedNetwork = true;
    const match = new RegExp(`/api/runs/${cancelled.id}/events\\?`);
    const handler = async (route) => {
      requests += 1;
      if (requests === 2) { failures += 1; await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"injected transient backlog failure"}' }); }
      else await route.continue();
    };
    await page.route(match, handler); await page.reload({ waitUntil: 'domcontentloaded' });
    await selected(cancelled.id); const final = await terminal(cancelled.id, 'CANCELLED');
    await page.unroute(match, handler); expectedNetwork = false;
    assert.equal(failures, 1); assert(requests >= 3); return { failed_reads: failures, total_reads: requests, cursor: final.cache.cursor };
  });
  let lost;
  await step('Lost submit response then refresh reuses exact body and Idempotency-Key', async () => {
    const before = (await json('/api/runs?limit=100')).items.length;
    let firstRequest; expectedNetwork = true;
    const drop = async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      firstRequest = { key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() };
      const response = await route.fetch({ maxRetries: 0 }); assert.equal(response.status(), 202);
      lost = await response.json(); await route.abort('failed');
    };
    await page.route(ORIGIN + '/api/runs', drop);
    await showForm('响应丢失后重试'); await page.getByRole('button', { name: '提交诊断任务', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '重试' }).waitFor();
    await page.unroute(ORIGIN + '/api/runs', drop);
    assert.equal((await json('/api/runs?limit=100')).items.length, before + 1);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: '新建诊断任务', exact: true }).click();
    await page.getByText(/已恢复上次未确认的提交/).waitFor();
    await waitUntil(() => page.getByLabel('申请人工交接工单').isChecked(), 'restored ticket checkbox after incident inputs load');
    const wait = page.waitForResponse((r) => r.url() === ORIGIN + '/api/runs' && r.request().method() === 'POST');
    await page.getByRole('button', { name: '提交诊断任务', exact: true }).click(); const response = await wait;
    assert.equal(response.status(), 202); assert.equal((await response.json()).id, lost.id);
    assert.equal(response.request().headers()['idempotency-key'], firstRequest.key);
    assert.deepEqual(response.request().postDataJSON(), firstRequest.body);
    assert.equal((await json('/api/runs?limit=100')).items.length, before + 1);
    report.run_ids.push(lost.id); await selected(lost.id); expectedNetwork = false;
    return { run_id: lost.id, same_key: true, same_body: true, created_runs: 1 };
  });
  let other;
  await step('Switching runs aborts late evidence response and preserves source identity', async () => {
    await worker(); const a = await waitApproval(lost.id);
    other = await submit('两任务隔离', 'dev-002'); await worker(); const b = await waitApproval(other.id);
    let release, seen = false; const gate = new Promise((resolve) => { release = resolve; });
    const refB = b.operation.params.references[0]; const refA = a.operation.params.references[0];
    const url = ORIGIN + `/api/runs/${other.id}/evidence/${refB.id}`;
    const delay = async (route) => { seen = true; const response = await route.fetch(); await gate; try { await route.fulfill({ response }); } catch { /* aborted on purpose */ } };
    await page.route(url, delay);
    await page.getByRole('button', { name: `查看证据 ${refB.id}`, exact: true }).first().click();
    await waitUntil(() => seen, 'delayed evidence request'); await select(lost.id);
    await page.getByRole('button', { name: `查看证据 ${refA.id}`, exact: true }).first().click();
    await page.getByLabel('证据快照原文').waitFor(); release(); await sleep(300); await page.unroute(url, delay);
    assert((await page.getByLabel('证据快照原文').innerText()).includes(refA.quote));
    assert(!(await page.getByLabel('证据快照原文').innerText()).includes(refB.quote));
    return { selected_run: lost.id, displayed_source: refA.id, rejected_late_source: refB.id };
  });
  await step('Action return after switching tasks does not approve or toast the new task', async () => {
    const url = ORIGIN + `/api/runs/${lost.id}/resume`; let release, seen = false;
    const gate = new Promise((resolve) => { release = resolve; });
    const delay = async (route) => { const response = await route.fetch(); seen = true; await gate; await route.fulfill({ response }); };
    await page.route(url, delay); await page.getByRole('button', { name: '批准当前工单草案' }).click();
    await waitUntil(() => seen, 'old task decision actually persisted'); await select(other.id);
    release(); await sleep(500); await page.unroute(url, delay);
    assert.equal((await getRun(other.id)).operation.decision, null);
    assert.equal(await page.getByRole('status').filter({ hasText: '批准已记录' }).count(), 0);
    await worker(); // completes the approved old task, never approves the current task
    assert.equal((await getRun(lost.id)).status, 'SUCCEEDED');
    assert.equal((await getRun(other.id)).status, 'WAITING_APPROVAL');
    return { completed_old_task: lost.id, current_still_pending: other.id };
  });
  await step('Stale list payload cannot regress an already observed terminal task', async () => {
    const match = new RegExp('/api/runs\\?limit=50&offset=0'); let injected = 0;
    const stale = async (route) => {
      const response = await route.fetch(); const value = await response.json();
      value.items = value.items.map((run) => run.id === first.id ? firstWaiting : run); injected += 1;
      await route.fulfill({ response, json: value });
    };
    await page.route(match, stale); await page.getByRole('button', { name: '刷新任务列表', exact: true }).click();
    await waitUntil(() => injected > 0, 'stale list delivered'); await sleep(200); await page.unroute(match, stale);
    const label = await page.getByRole('button', { name: new RegExp(`选择任务 ${first.id.slice(0, 8)}`) }).getAttribute('aria-label');
    assert(label.includes('已完成')); return { older_revision: firstWaiting.revision, terminal_revision: (await getRun(first.id)).revision, visible_label: label };
  });
  await step('Mobile layout has no horizontal overflow and evidence drawer remains usable', async () => {
    await page.setViewportSize({ width: 390, height: 844 }); await select(first.id);
    await page.getByRole('button', { name: `查看证据 ${firstWaiting.operation.params.references[0].id}`, exact: true }).first().click();
    await page.getByLabel('证据快照原文').waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const drawer = page.getByRole('dialog', { name: '来源证据', exact: true });
    await drawer.focus(); await page.keyboard.press('Tab');
    const closeButton = page.getByRole('button', { name: '关闭来源证据', exact: true });
    assert(await closeButton.evaluate((element) => element === document.activeElement));
    await page.keyboard.press('Tab');
    assert(!(await closeButton.evaluate((element) => element === document.activeElement)), 'Tab must reach source link, not be trapped on close');
    await page.keyboard.press('Shift+Tab'); await page.keyboard.press('Shift+Tab');
    assert(!(await closeButton.evaluate((element) => element === document.activeElement)), 'Shift+Tab from first wraps to last source button');
    await page.keyboard.press('Tab');
    assert(await closeButton.evaluate((element) => element === document.activeElement));
    await screenshot('mobile-source'); await closeButton.click();
    await screenshot('mobile-completed'); await page.setViewportSize({ width: 1520, height: 1020 });
    return { viewport_width: 390, horizontal_overflow: false };
  });
  await step('Final database reconciliation proves one ticket per approved run and zero real model API calls', async () => {
    const code = `import json,sqlite3,sys; from pathlib import Path\nwith sqlite3.connect(Path(sys.argv[1])/'app.db') as c:\n c.row_factory=sqlite3.Row\n print(json.dumps({'runs':[dict(r) for r in c.execute('select id,status,active_started_at from runs')],'tickets':[dict(r) for r in c.execute('select run_id,operation_id,id from tickets')],'receipts':c.execute('select count(*) from tool_receipts').fetchone()[0],'effect_events':c.execute(\"select count(*) from events where kind='ticket.created'\").fetchone()[0],'unapproved':c.execute(\"select count(*) from tickets t left join operations o on t.operation_id=o.id where o.id is null or coalesce(o.decision,'')!='approve'\").fetchone()[0],'real_api_calls':c.execute(\"select count(*) from model_calls where scope!='reliability-fake'\").fetchone()[0]}))`;
    const counts = JSON.parse((await python(code, [localDir])).stdout);
    assert.equal(counts.tickets.length, 2); assert.equal(counts.receipts, 2); assert.equal(counts.effect_events, 2);
    assert.equal(counts.unapproved, 0); assert.equal(counts.real_api_calls, 0);
    assert.equal(new Set(counts.tickets.map((t) => t.run_id)).size, 2);
    assert.equal(counts.tickets.find((t) => t.run_id === first.id).id, firstTicket);
    report.database_counts = counts; await screenshot('desktop-final'); return counts;
  });
  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.console_errors, []);
  if (recordDemo) {
    // Reading time is deliberate, showing real persisted report/effect/event data.
    // Nothing is injected into the page, and the recording never simulates progress.
    const sections = ['辅助诊断', '人工交接', '调用与用量', '关键事件'];
    let index = 0;
    while (Date.now() - Date.parse(report.started_at) < 195000) {
      await page.getByRole('heading', { name: sections[index++ % sections.length], exact: true }).scrollIntoViewIfNeeded();
      await sleep(Math.min(12000, Math.max(0, 195000 - (Date.now() - Date.parse(report.started_at)))));
    }
  }
  report.status = 'PASSED';
} catch (error) {
  report.status = 'FAILED'; report.error = { type: error.name, message: error.message };
  if (page) { try { await screenshot('failure'); report.failure_dom = await page.locator('body').innerText(); } catch {} }
  console.error(JSON.stringify(report.error)); process.exitCode = 1;
} finally {
  report.finished_at = new Date().toISOString();
  report.elapsed_seconds = (Date.parse(report.finished_at) - Date.parse(report.started_at)) / 1000;
  if (context) await context.close();
  if (video) { report.video = path.join(OUT, 'demo.webm'); await video.saveAs(report.video); }
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    server.kill('SIGTERM'); await new Promise((resolve) => server.once('close', resolve));
  }
  await writeFile(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  await rm(localDir, { recursive: true, force: true });
  console.log(JSON.stringify({ status: report.status, report: path.join(OUT, 'report.json'), elapsed_seconds: report.elapsed_seconds, video: report.video || null }));
}
