import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright';

// Run against an isolated Vite server. Every API request is intercepted; these
// checks never contact a model, read a real project, or approve a real action.
const base = process.env.INCIDENTDESK_UI_URL || 'http://127.0.0.1:5174';
const profile = { version: 3, provider: 'deepseek', base_url: 'https://api.deepseek.com', model: 'test-model', reasoning: 'off', max_output_tokens: 2048, request_timeout_seconds: 30, active_seconds: 180, model_call_limit: 6, tool_call_limit: 20, run_budget_cny: 2, project_budget_cny: 300, max_request_bytes: 262144, pricing: { input_per_million_cny: 2, output_per_million_cny: 8, cache_read_per_million_cny: 0.04 }, has_credential: true, experimental: false };
const settings = { active_version: 3, profile, capabilities: { reasoning: ['off', 'low', 'high', 'max'] } };
const project = { id: 'socialengine', name: 'SocialEngine', root: '/local/SocialEngine', log_files: ['logs/app.log'], config_files: ['config.json'], start_argv: ['python3', 'app.py'], health_url: 'http://127.0.0.1:8000/health', metrics_url: null, stats_url: null, timezone: 'Asia/Shanghai', version: 2, created_at: '2026-11-12T00:00:00Z' };
const reference = { id: 'evidence-one', version: '1', quote: 'upstream timed out' };
const makeRun = (overrides = {}) => ({ id: 'run-one', status: 'WAITING_APPROVAL', revision: 2, request: { mode: 'live', question: '检查本机服务最近的异常', service_id: project.id, incident_id: null, project_version: 2, model_profile_version: 3, start: '2026-11-12T00:00:00Z', end: '2026-11-12T00:30:00Z' }, diagnosis: null, operation: { id: 'operation-one', operation_id: 'operation-one', interrupt_id: 'interrupt-one', kind: 'service_action', draft_version: 1, params_digest: 'digest-one', params: { request: { action: 'update_config', path: 'config.json', reason: '核对后调整超时配置', references: [reference] }, preview: { title: '更新超时配置', target: 'config.json', diff: '- timeout: 1\n+ timeout: 3', before_sha256: 'before-hash', warnings: ['仅修改已注册的配置文件'] } }, decision: null, status: 'PENDING', created_at: '2026-11-12T00:00:00Z', decided_at: null }, ticket_id: null, action_receipt: null, cancel_requested: false, usage: { model_calls: 2, input_tokens: 300, output_tokens: 200, cache_read_tokens: 0, known_cost_cny: 0.002, unreconciled_reservations_cny: 0, unknown_usage_calls: 0 }, active_seconds: 2, active_started_at: null, tool_calls: 2, created_at: '2026-11-12T00:31:00Z', updated_at: '2026-11-12T00:31:00Z', error: null, ...overrides });
async function workspace(runs = [], options = {}) {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const calls = [];
  const indexed = new Set();
  const availableProjects = options.projects || [{ ...project, knowledge_paths: ['runbooks'], retrieval_mode: 'hybrid', history_enabled: true, business_probe: { method: 'POST', path: '/query', body: { type: 'dist', a: 1, b: 1 }, expected_status: 200, expected_json: { distance: 0 } } }];
  await context.route('**/api/**', async (route) => {
    const req = route.request(); const path = new URL(req.url()).pathname; const method = req.method();
    const body = req.postDataJSON?.(); calls.push({ path, method, body });
    const override = await options.route?.({ path, method, body });
    if (override !== undefined) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(override) });
    let response;
    if (path === '/api/runs' && method === 'GET') response = { items: runs, limit: 50, offset: 0 };
    else if (path === '/api/runs' && method === 'POST') response = makeRun({ id: 'new-run', request: body, status: 'QUEUED', operation: null });
    else if (path === '/api/projects') response = { items: availableProjects };
    else if (path.endsWith('/knowledge')) {
      const id = path.split('/')[3];
      response = { project_id: id, project_version: 2, active_generation: 'a'.repeat(64), usable_generation: 'a'.repeat(64), generations: [{ generation: 'a'.repeat(64), project_version: 2, documents: 2, chunks: 4, created_at: project.created_at, embedding: { indexed: indexed.has(id), state: indexed.has(id) ? 'ready' : 'disabled', fingerprint: 'test' } }], model: { state: 'ready', model_id: 'local-test-model', revision: 'test' } };
    }
    else if (path.endsWith('/knowledge/index')) { indexed.add(path.split('/')[3]); response = { generation: 'a'.repeat(64), chunks: 4 }; }
    else if (path.endsWith('/knowledge/search')) response = { status: 'degraded', error: { code: 'embedding_unavailable', message: 'Local embedding inference failed' }, retrieval: { requested_mode: 'hybrid', effective_mode: 'lexical' }, items: [{ id: 'knowledge-one', content: 'Compare saturation with the rejection counter.', version: '1', provenance: { project_id: path.split('/')[3], relative_path: 'runbooks/queue.md', line_start: 4, line_end: 9 } }] };
    else if (path.endsWith('/sample')) response = { status: 'complete' };
    else if (path.endsWith('/observations')) response = { status: 'partial', coverage: { samples_in_window: 1, leading_gap: true, complete_window: false }, items: [{ id: 'history-one', content: '{"counter_resets":1,"rate_per_second":null}', version: '1' }] };
    else if (path.endsWith('/probe-business')) response = { status: 'complete', business_ok: false, items: [{ id: 'probe-one', content: '{"http_status":200,"assertions_passed":false}', version: '1' }] };
    else if (path === '/api/model-settings') response = settings;
    else if (path === '/api/incidents') response = { items: [{ question: '检查模拟案例', service_id: 'checkout-api', incident_id: 'dev-001', start: '2026-10-01T00:00:00Z', end: '2026-10-01T00:30:00Z' }] };
    else if (path === '/api/readiness') response = { api: 'ready', worker: { status: 'stale', mode: 'live' }, model: { status: 'configured', active_version: 3, note: 'not a connection test' }, projects: 1 };
    else if (path.endsWith('/events/stream')) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    else if (path.endsWith('/events')) response = { items: [], next_cursor: 0 };
    else if (path.endsWith('/resume')) response = { accepted: true, operation: runs[0]?.operation };
    else if (path.endsWith('/outcome')) response = { outcome: null };
    else if (path.endsWith('/verify-business')) response = { items: [] };
    else if (path.includes('/evidence/')) response = { ...reference, content: 'upstream timed out', source: 'logs' };
    else if (path.startsWith('/api/runs/')) response = runs.find((item) => path.endsWith(item.id)) || makeRun({ id: 'new-run', status: 'QUEUED', operation: null });
    else return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unmocked endpoint"}' });
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(response) });
  });
  const page = await context.newPage(); await page.goto(base);
  await page.getByLabel('本机就绪状态').getByText('后台执行：心跳过期').waitFor();
  return { browser, page, calls };
}

test('new tasks default to real project and a 30 minute window, without selecting a fixture', async () => {
  const { browser, page, calls } = await workspace();
  try {
    await page.getByRole('button', { name: '新建诊断任务', exact: true }).click();
    assert.equal(await page.getByLabel('任务来源').inputValue(), 'live');
    assert.equal(await page.getByLabel('真实项目').inputValue(), 'socialengine');
    assert.equal(await page.getByLabel('选择模拟案例').count(), 0);
    assert.equal(await page.getByLabel('申请人工交接工单').count(), 0);
    await page.waitForFunction(() => document.querySelector('[aria-label="开始时间"]')?.value && document.querySelector('[aria-label="结束时间"]')?.value);
    const start = await page.getByLabel('开始时间', { exact: true }).inputValue();
    const end = await page.getByLabel('结束时间', { exact: true }).inputValue();
    assert.equal((new Date(end) - new Date(start)) / 60000, 30);
    await page.getByLabel('异常描述').fill('请核对最近服务响应变慢的原因');
    await page.getByRole('button', { name: '提交诊断任务', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    const sent = calls.find((item) => item.path === '/api/runs' && item.method === 'POST').body;
    assert.equal(sent.mode, 'live'); assert.equal(sent.incident_id, null);
    assert.equal(sent.project_version, 2); assert.equal(sent.model_profile_version, 3);
    assert.equal(sent.service_id, 'socialengine'); assert.equal(sent.parent_run_id, null);
  } finally { await browser.close(); }
});

test('saved retrieval and cloud settings are used for project searches', async () => {
  const { browser, page, calls } = await workspace([], {
    route({ path, method, body }) {
      if (path === '/api/projects/socialengine' && method === 'PUT') {
        return { ...body, version: 3, created_at: project.created_at };
      }
    },
  });
  try {
    await page.getByRole('button', { name: '项目设置', exact: true }).click();
    await page.getByLabel('编辑项目').selectOption(project.id);
    await page.getByLabel('每路召回候选数').fill('30');
    await page.getByLabel('重排候选数').fill('12');
    await page.getByLabel('最终片段数').fill('4');
    await page.getByLabel('候选重排').selectOption('cross_encoder');
    await page.getByText('云端数据发送规则', { exact: true }).click();
    await page.getByLabel('云端诊断策略').selectOption('deny');
    await page.getByRole('button', { name: '保存项目配置' }).click();
    await page.getByText('已保存项目 v3 · 知识库与运行历史').waitFor();
    const save = calls.find(({ method, path }) => method === 'PUT' && path === '/api/projects/socialengine');
    assert.deepEqual(save.body.retrieval_config, { candidate_n: 30, rerank_top_n: 12, top_k: 4, reranker: 'cross_encoder' });
    assert.equal(save.body.cloud_policy.mode, 'deny');
    await page.getByLabel('知识库试检索').fill('队列饱和');
    await page.getByRole('button', { name: '检索已冻结知识版本' }).click();
    const search = calls.find(({ path }) => path.endsWith('/knowledge/search'));
    assert.equal(search.body.project_version, 3);
    assert.equal('top_k' in search.body, false);
  } finally {
    await browser.close();
  }
});

test('business verification attaches evidence before saving operator outcome', async () => {
  const terminal = makeRun({ status: 'SUCCEEDED', operation: null });
  const probe = { id: 'verified-probe', source: 'business_probe', version: 'v-probe', content: '{"business_ok": true}' };
  const { page, calls, browser } = await workspace([terminal], {
    route({ path, method, body }) {
      if (path.endsWith('/verify-business') && method === 'POST') return { business_ok: true, items: [probe] };
      if (path.endsWith('/outcome') && method === 'POST') return { outcome: { ...body, revision: 1 } };
    },
  });
  try {
    await page.getByRole('button', { name: '运行只读业务检查并保存证据' }).click();
    await page.getByText('登记的业务断言通过。', { exact: false }).waitFor();
    await page.getByText('尚未记录 · 填写本次核对', { exact: true }).click();
    await page.getByLabel('诊断核对', { exact: true }).selectOption('inconclusive');
    await page.getByLabel('诊断核对依据').fill('当前只确认业务断言通过');
    await page.getByLabel('业务恢复', { exact: true }).selectOption('verified');
    await page.getByLabel('业务核对依据').fill('登记断言与实际响应一致');
    await page.getByRole('checkbox', { name: /business_ok/ }).check();
    await page.getByRole('button', { name: '保存人工核对' }).click();
    await page.getByText('已保存人工核对 v1', { exact: false }).waitFor();
    const outcome = calls.find(({ path, method }) => path.endsWith('/outcome') && method === 'POST');
    assert.deepEqual(outcome.body.verification_references, [{ id: 'verified-probe', version: 'v-probe', quote: '"business_ok": true' }]);
    assert.equal(outcome.body.expected_revision, 0);
  } finally {
    await browser.close();
  }
});

test('model secret input is write-only and discarded when settings close; unsaved config blocks tests', async () => {
  const { browser, page, calls } = await workspace();
  try {
    await page.getByRole('button', { name: '模型设置', exact: true }).click();
    await page.getByLabel('API 密钥', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('API 密钥', { exact: true }).inputValue(), '');
    await page.getByLabel('API 密钥', { exact: true }).fill('fake-ui-only-secret');
    assert.equal(await page.getByRole('button', { name: '验证连接与工具调用（计费）' }).isDisabled(), true);
    await page.getByRole('button', { name: '关闭设置' }).click();
    await page.getByRole('button', { name: '模型设置', exact: true }).click();
    assert.equal(await page.getByLabel('API 密钥', { exact: true }).inputValue(), '');
    const browserStorage = await page.evaluate(() => JSON.stringify({ ...localStorage }));
    assert.equal(browserStorage.includes('fake-ui-only-secret'), false);
    assert.equal(calls.some((item) => item.path.endsWith('/test') || item.path.endsWith('/models')), false);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({ path: '/tmp/incidentdesk-model-settings-mobile.png', fullPage: true });
  } finally { await browser.close(); }
});

test('service config preview uses frozen approval fields and never claims recovery on approval', async () => {
  const { browser, page, calls } = await workspace([makeRun()]);
  try {
    await page.getByLabel('配置变更差异').waitFor();
    await page.screenshot({ path: '/tmp/incidentdesk-action-preview.png', fullPage: true });
    assert.match(await page.getByLabel('配置变更差异').innerText(), /timeout: 3/);
    await page.getByRole('button', { name: '批准当前服务操作' }).click();
    await page.getByRole('status').filter({ hasText: '批准已记录' }).waitFor();
    const approval = calls.find((item) => item.path.endsWith('/resume')).body;
    assert.deepEqual(approval, { interrupt_id: 'interrupt-one', operation_id: 'operation-one', draft_version: 1, params_digest: 'digest-one', decision: 'approve' });
    assert.equal('params' in approval, false);
    assert.equal(await page.getByText('进程与健康端点核查已通过；业务恢复仍需结合实际请求与指标确认。').count(), 0);
    await page.getByRole('button', { name: '查看证据 evidence-one', exact: true }).first().click();
    await page.getByLabel('证据快照原文').waitFor();
    assert.match(await page.getByLabel('证据快照原文').innerText(), /upstream timed out/);
  } finally { await browser.close(); }
});

test('follow-up keeps parent ID and service while refreshing the evidence window', async () => {
  const terminal = makeRun({ status: 'SUCCEEDED', operation: null });
  const { browser, page, calls } = await workspace([terminal]);
  try {
    await page.getByRole('button', { name: '基于此任务追问' }).click();
    await page.getByLabel('异常描述').fill('请继续核对最近的日志，检查故障是否已经恢复');
    await page.getByRole('button', { name: '提交诊断任务', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    const sent = calls.find((item) => item.path === '/api/runs' && item.method === 'POST').body;
    assert.equal(sent.parent_run_id, terminal.id); assert.equal(sent.mode, 'live');
    assert.equal(sent.service_id, terminal.request.service_id);
    assert.equal((new Date(sent.end) - new Date(sent.start)) / 60000, 30);
  } finally { await browser.close(); }
});

test('project knowledge, history gaps and failed business assertions remain explicit', async () => {
  const { browser, page, calls } = await workspace();
  try {
    await page.getByRole('button', { name: '项目设置', exact: true }).click();
    await page.getByLabel('编辑项目').selectOption('socialengine');
    await page.getByLabel('知识文档', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('知识文档', { exact: true }).inputValue(), 'runbooks');
    assert.equal(await page.getByLabel('知识检索方式').inputValue(), 'hybrid');
    await page.getByText('2 个文档 · 4 个片段', { exact: false }).waitFor();
    await page.getByRole('status').filter({ hasText: '当前知识版本没有向量索引' }).waitFor();
    assert.equal(calls.some((item) => /index|search|sample|probe-business/.test(item.path)), false);
    await page.getByRole('button', { name: '建立 / 更新知识索引' }).click();
    await page.getByRole('status').filter({ hasText: '已建立 4 个知识片段' }).waitFor();
    assert.equal(await page.getByText('当前知识版本没有向量索引', { exact: false }).count(), 0);
    assert.deepEqual(calls.find((item) => item.path.endsWith('/knowledge/index')).body, { project_version: 2, embed: true });
    await page.getByLabel('知识库试检索').fill('queue saturation');
    await page.getByRole('button', { name: '检索已冻结知识版本' }).click();
    await page.getByText('检索：degraded · 实际方式 lexical').waitFor();
    assert.equal(await page.getByLabel('知识片段来源').innerText(), 'runbooks/queue.md:4–9');
    await page.getByText('错误代码：embedding_unavailable', { exact: false }).waitFor();
    await page.getByText('已降级为词法检索', { exact: false }).waitFor();
    assert.equal(calls.find((item) => item.path.endsWith('/knowledge/search')).body.generation, 'a'.repeat(64));
    await page.getByRole('button', { name: '采样并查看最近 30 分钟' }).click();
    await page.getByText('历史覆盖：partial · 1 个样本').waitFor();
    await page.getByRole('button', { name: '执行一次已登记业务探针' }).click();
    await page.getByText('业务探针：未通过 / 不可用').waitFor();
    assert.equal(calls.filter((item) => item.path.endsWith('/probe-business')).length, 1);
    assert.equal(calls.some((item) => item.path === '/api/runs' && item.method === 'POST'), false);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({ path: '/tmp/incidentdesk-knowledge-history-mobile.png', fullPage: true });
  } finally { await browser.close(); }
});

test('a delayed result from a previous project never populates the selected project', async () => {
  let finishSearch; let startedSearch;
  const searchDone = new Promise((resolve) => { finishSearch = resolve; });
  const searchStarted = new Promise((resolve) => { startedSearch = resolve; });
  const projects = [project, { ...project, id: 'other-service', name: 'Other service', root: '/local/Other' }].map((item) => ({ ...item, knowledge_paths: ['runbooks'], retrieval_mode: 'hybrid' }));
  const { browser, page } = await workspace([], { projects, route: async ({ path }) => {
    if (path === '/api/projects/socialengine/knowledge/search') {
      startedSearch(); await searchDone;
      return { status: 'complete', retrieval: { requested_mode: 'hybrid', effective_mode: 'hybrid' }, items: [{ id: 'old-project-result', version: '1', content: 'STALE_PROJECT_RESULT' }] };
    }
  } });
  try {
    await page.getByRole('button', { name: '项目设置', exact: true }).click();
    await page.getByLabel('编辑项目').selectOption('socialengine');
    await page.getByText('2 个文档 · 4 个片段', { exact: false }).waitFor();
    await page.getByLabel('知识库试检索').fill('queue');
    await page.getByRole('button', { name: '检索已冻结知识版本' }).click();
    await searchStarted;
    await page.getByLabel('编辑项目').selectOption('other-service');
    await page.getByText('2 个文档 · 4 个片段', { exact: false }).waitFor();
    const response = page.waitForResponse((value) => value.url().endsWith('/socialengine/knowledge/search'));
    finishSearch(); await response;
    await page.waitForTimeout(80);
    assert.equal(await page.getByLabel('本机项目目录').inputValue(), '/local/Other');
    assert.equal(await page.getByLabel('知识库试检索').inputValue(), '');
    assert.equal(await page.getByText('STALE_PROJECT_RESULT').count(), 0);
    assert.equal(await page.getByRole('button', { name: '建立 / 更新知识索引' }).isEnabled(), true);
  } finally { finishSearch(); await browser.close(); }
});

test('closing and reopening settings while indexing ignores the old completion', async () => {
  let finishIndex; let startedIndex;
  const indexDone = new Promise((resolve) => { finishIndex = resolve; });
  const indexStarted = new Promise((resolve) => { startedIndex = resolve; });
  const { browser, page, calls } = await workspace([], { route: async ({ path }) => {
    if (path.endsWith('/knowledge/index')) {
      startedIndex(); await indexDone;
      return { generation: 'b'.repeat(64), chunks: 99 };
    }
  } });
  try {
    await page.getByRole('button', { name: '项目设置', exact: true }).click();
    await page.getByLabel('编辑项目').selectOption('socialengine');
    await page.getByRole('button', { name: '建立 / 更新知识索引' }).click();
    await indexStarted;
    await page.getByText('关闭设置不会取消正在建立的索引', { exact: false }).waitFor();
    await page.getByRole('button', { name: '关闭设置' }).click();
    await page.getByRole('button', { name: '项目设置', exact: true }).click();
    await page.getByText('2 个文档 · 4 个片段', { exact: false }).waitFor();
    const readsBefore = calls.filter((item) => item.path.endsWith('/knowledge')).length;
    const response = page.waitForResponse((value) => value.url().endsWith('/knowledge/index'));
    finishIndex(); await response;
    await page.waitForTimeout(80);
    assert.equal(await page.getByRole('status').filter({ hasText: '已建立 99 个知识片段' }).count(), 0);
    assert.equal(calls.filter((item) => item.path.endsWith('/knowledge')).length, readsBefore);
    assert.equal(await page.getByRole('button', { name: '建立 / 更新知识索引' }).isEnabled(), true);
  } finally { finishIndex(); await browser.close(); }
});
