export type RunStatus = 'QUEUED' | 'RUNNING' | 'WAITING_APPROVAL' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
export interface RunInput {
  question: string;
  mode?: 'live' | 'fixture';
  service_id: string;
  incident_id?: string | null;
  project_version?: number | null;
  model_profile_version?: number | null;
  knowledge_generation?: string | null;
  retrieval_mode?: 'lexical' | 'dense' | 'hybrid' | null;
  parent_run_id?: string | null;
  start: string;
  end: string;
}
export interface Incident extends RunInput { window: { start: string; end: string } }
export interface Reference { id: string; version: string; quote: string }
export interface TicketDraft { title: string; description: string; priority: 'low' | 'medium' | 'high' }
export interface Diagnosis {
  summary: string;
  hypotheses: string[];
  supporting_evidence: Reference[];
  contradicting_evidence: Reference[];
  missing_information: string[];
  recommended_checks: string[];
  needs_human: boolean;
  ticket_draft: TicketDraft | null;
}
export interface Operation {
  id: string;
  operation_id: string;
  interrupt_id: string;
  draft_version: number;
  params_digest: string;
  kind?: 'create_ticket' | 'service_action';
  params: {
    draft?: TicketDraft; references?: Reference[];
    request?: { action: 'restart_service' | 'update_config'; path?: string; expected_sha256?: string; content?: string; reason: string; references: Reference[] };
    preview?: { title: string; command?: string[]; diff?: string; before_sha256?: string; target: string; warnings: string[] };
  };
  decision: 'approve' | 'reject' | null;
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'COMMITTED';
  created_at: string;
  decided_at: string | null;
}
export interface Usage {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  known_cost_cny: number;
  unreconciled_reservations_cny: number;
  unknown_usage_calls: number;
}
export interface Run {
  id: string;
  status: RunStatus;
  revision: number;
  request: RunInput;
  diagnosis: Diagnosis | null;
  operation: Operation | null;
  ticket_id: string | null;
  action_receipt?: ActionReceipt | null;
  cancel_requested: boolean;
  usage: Usage;
  active_seconds: number;
  active_started_at: string | null;
  tool_calls: number;
  report_repairs?: number;
  created_at: string;
  updated_at: string;
  error: { type?: string; message?: string; code?: string } | string | null;
}
export interface BusinessEvent {
  event_id: number;
  id: number;
  run_id: string;
  event_key: string;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}
export interface EvidenceSnapshot {
  id: string;
  version: string | number;
  content: unknown;
  source?: string;
  timestamp?: string;
  observed_at?: string;
  doc_id?: string;
  doc_version?: string;
  title?: string;
  service_id?: string;
  provenance?: { relative_path?: string; line_start?: number; line_end?: number; [key: string]: unknown };
  [key: string]: unknown;
}
export interface ApprovalPayload {
  interrupt_id: string;
  operation_id: string;
  draft_version: number;
  params_digest: string;
  decision: 'approve' | 'reject';
}
export interface ProjectProfile {
  id: string; name: string; root: string; log_files: string[];
  health_url: string | null; metrics_url: string | null; stats_url: string | null;
  start_argv: string[]; config_files: string[]; timezone: string; version: number; created_at: string;
  knowledge_paths?: string[];
  retrieval_mode?: 'lexical' | 'dense' | 'hybrid';
  history_enabled?: boolean;
  business_probe?: BusinessProbeProfile | null;
}
export interface BusinessProbeProfile {
  method: 'GET' | 'POST'; path: string; body: Record<string, unknown>;
  expected_status: number; expected_json: Record<string, unknown>; max_latency_ms?: number | null;
}
export interface EvidenceBatch {
  status: string; items: EvidenceSnapshot[]; coverage?: Record<string, unknown>;
  retrieval?: { requested_mode: string; effective_mode: string; latency_ms?: number };
  error?: { code?: string; message?: string } | string | null;
  [key: string]: unknown;
}
export interface KnowledgeStatus {
  project_id: string; project_version: number; usable_generation: string | null;
  active_generation: string | null;
  generations: { generation: string; project_version: number; documents: number; chunks: number; created_at: string; embedding: { indexed: boolean; state: string; fingerprint: string } }[];
  model: { state: string; model_id: string; revision: string; download_required?: boolean };
}
export type ProjectInput = Omit<ProjectProfile, 'version' | 'created_at'> & { expected_version?: number };
export interface ProjectProbe {
  project_id: string; project_version: number;
  items: { source: string; status: string; error?: string | null; items: EvidenceSnapshot[] }[];
}
export type Reasoning = 'off' | 'low' | 'high' | 'max';
export interface ModelProfile {
  version: number; provider: 'deepseek'; base_url: string; model: string; reasoning: Reasoning;
  max_output_tokens: number; request_timeout_seconds: number; active_seconds: number;
  model_call_limit: number; tool_call_limit: number; run_budget_cny: number; project_budget_cny: number;
  max_request_bytes: number;
  pricing: { input_per_million_cny: number; output_per_million_cny: number; cache_read_per_million_cny: number };
  has_credential: boolean; experimental: boolean;
}
export interface ModelSettings { active_version: number; profile: ModelProfile; capabilities: { reasoning: Reasoning[] } }
export type ModelInput = Omit<ModelProfile, 'version' | 'has_credential' | 'experimental'> & { expected_version?: number; api_key?: string; clear_credential?: boolean };
export interface ModelTest {
  version: number; ok: boolean; tool_roundtrip: boolean; requested_model: string; returned_model: string | null;
  model_calls: number; known_cost_cny: number; unreconciled_reservations_cny: number; error_type?: string; message: string;
}
export interface Readiness {
  api: 'ready'; worker: { status: 'ready' | 'stale' | 'unavailable'; pid?: number; at?: string; age_seconds?: number; mode?: 'live' | 'offline' };
  sampler?: { status: 'ready' | 'degraded' | 'error' | 'idle' | 'stale' | 'unavailable'; counts?: Record<string, number>; age_seconds?: number };
  model: { status: 'configured' | 'unconfigured'; active_version?: number; note: string }; projects: number;
}
export interface ActionReceipt {
  operation_id: string; run_id: string; status: 'STARTED' | 'SUCCEEDED' | 'FAILED' | 'UNKNOWN'; created_at?: string; updated_at?: string;
  result: { action?: string; pid?: number; health_before?: { status: string; http_status?: number }; health_after?: { status: string; http_status?: number };
    recovery_verified?: boolean; listener_verified?: boolean; path?: string; before_sha256?: string; after_sha256?: string; backup_id?: string; message?: string; error_type?: string };
}
