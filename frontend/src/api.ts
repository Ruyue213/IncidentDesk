import type {
  ApprovalPayload, BusinessEvent, EvidenceBatch, EvidenceSnapshot, Incident, KnowledgeStatus, ModelInput, ModelSettings, ModelTest, Operation, Outcome, OutcomeAggregate, OutcomeInput, ProjectInput, ProjectProbe, ProjectProfile, Readiness, Run, RunInput,
} from './types.ts';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: 'no-store', ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((item: { msg?: string }) => item.msg || '输入校验失败').join('；') : '请求未完成';
    throw new ApiError(response.status, message);
  }
  return data as T;
}
const runPath = (id: string) => `/api/runs/${encodeURIComponent(id)}`;
export const api = {
  health: (signal?: AbortSignal) => request<{ status: string }>('/health', { signal }),
  readiness: () => request<Readiness>('/api/readiness'),
  incidents: () => request<{ items: Incident[] }>('/api/incidents'),
  projects: () => request<{ items: ProjectProfile[] }>('/api/projects'),
  saveProject: (id: string, body: ProjectInput) => request<ProjectProfile>(`/api/projects/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) }),
  probeProject: (id: string) => request<ProjectProbe>(`/api/projects/${encodeURIComponent(id)}/probe`, { method: 'POST' }),
  knowledge: (id: string) => request<KnowledgeStatus>(`/api/projects/${encodeURIComponent(id)}/knowledge`),
  indexKnowledge: (id: string, version: number) => request<{ generation: string; chunks: number }>(`/api/projects/${encodeURIComponent(id)}/knowledge/index`, { method: 'POST', body: JSON.stringify({ project_version: version, embed: true }) }),
  searchKnowledge: (id: string, generation: string, query: string, mode: string, version?: number) => request<EvidenceBatch>(`/api/projects/${encodeURIComponent(id)}/knowledge/search`, { method: 'POST', body: JSON.stringify({ generation, query, mode, ...(version ? { project_version: version } : {}) }) }),
  sampleProject: (id: string) => request<Record<string, unknown>>(`/api/projects/${encodeURIComponent(id)}/sample`, { method: 'POST' }),
  observations: (id: string, version: number, start: string, end: string) => request<EvidenceBatch>(`/api/projects/${encodeURIComponent(id)}/observations?${new URLSearchParams({ version: String(version), start, end, limit: '100' })}`),
  businessProbe: (id: string) => request<EvidenceBatch>(`/api/projects/${encodeURIComponent(id)}/probe-business`, { method: 'POST' }),
  modelSettings: () => request<ModelSettings>('/api/model-settings'),
  saveModel: (body: ModelInput) => request<ModelSettings>('/api/model-settings', { method: 'PUT', body: JSON.stringify(body) }),
  models: (version: number) => request<{ version: number; models: { id: string }[]; inference_called: false }>('/api/model-settings/models', { method: 'POST', body: JSON.stringify({ version }) }),
  testModel: (version: number) => request<ModelTest>('/api/model-settings/test', { method: 'POST', body: JSON.stringify({ version, allow_paid: true }) }),
  runs: (limit = 50, offset = 0) => request<{ items: Run[]; limit: number; offset: number }>(`/api/runs?limit=${limit}&offset=${offset}`),
  run: (id: string, signal?: AbortSignal) => request<Run>(runPath(id), { signal }),
  submit: (body: RunInput, key: string) => request<Run>('/api/runs', {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body),
  }),
  decide: (id: string, body: ApprovalPayload) => request<{ accepted: boolean; operation: Operation }>(`${runPath(id)}/resume`, {
    method: 'POST', body: JSON.stringify(body),
  }),
  cancel: (id: string) => request<Run>(`${runPath(id)}/cancel`, { method: 'POST' }),
  events: (id: string, cursor: number, signal?: AbortSignal) => request<{ items: BusinessEvent[]; next_cursor: number }>(`${runPath(id)}/events?after=${cursor}&limit=200`, { signal }),
  evidence: (id: string, evidenceId: string, signal?: AbortSignal) => request<EvidenceSnapshot>(`${runPath(id)}/evidence/${encodeURIComponent(evidenceId)}`, { signal }),
  outcome: (id: string) => request<{ outcome: Outcome | null }>(`${runPath(id)}/outcome`),
  saveOutcome: (id: string, body: OutcomeInput) => request<{ outcome: Outcome }>(`${runPath(id)}/outcome`, { method: 'POST', body: JSON.stringify(body) }),
  outcomeAggregate: () => request<OutcomeAggregate>('/api/outcomes/aggregate?mode=live'),
  businessVerifications: (id: string) => request<{ items: EvidenceSnapshot[] }>(`${runPath(id)}/verify-business`),
  verifyBusiness: (id: string) => request<EvidenceBatch>(`${runPath(id)}/verify-business`, { method: 'POST' }),
  retrievalTrace: (id: string) => request<Record<string, unknown>[]>(`${runPath(id)}/retrieval`),
  cloudAudit: (id: string) => request<{ items: Record<string, unknown>[] }>(`${runPath(id)}/cloud-audit`),
  streamPath: (id: string, cursor: number) => `${runPath(id)}/events/stream?cursor=${cursor}`,
};
