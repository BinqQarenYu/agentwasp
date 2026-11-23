export type AgentStatus =
  | 'registering'
  | 'running'
  | 'idle'
  | 'paused'
  | 'blocked'
  | 'degraded'
  | 'failed'
  | 'offline'
  | 'awaiting_approval'
  | 'safe_stopping'
  | 'disabled';

export type AgentHealth = 'healthy' | 'degraded' | 'api_issue' | 'offline' | 'stale';

export type RiskLevel = 'low' | 'medium' | 'high' | 'critical';

export type TaskStatus =
  | 'draft'
  | 'queued'
  | 'assigned'
  | 'running'
  | 'blocked'
  | 'awaiting_approval'
  | 'retry_scheduled'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type ActorRole = 'supervisor' | 'operator' | 'auditor' | 'viewer';

export type AgentCommandType =
  | 'pause'
  | 'resume'
  | 'cancel'
  | 'safe_stop'
  | 'retry'
  | 'disable'
  | 'rollback'
  | 'request_approval';

export interface SkillBinding {
  skill_id: string;
  name: string;
  version: string;
  enabled: boolean;
  test_status: 'passing' | 'failing' | 'untested';
  last_verified_at: string;
}

export interface ToolBinding {
  tool_id: string;
  name: string;
  scope: string;
  enabled: boolean;
  integration_id?: string;
  integration_health: 'healthy' | 'degraded' | 'api_issue' | 'offline';
}

export interface MemoryPolicy {
  retention_days: number;
  provenance_required: boolean;
  access_scope: 'tenant_team' | 'agent_private' | 'shared_knowledge';
  redaction_enabled: boolean;
  last_consolidation_at: string;
  working_memory_items: number;
  episodic_records: number;
  knowledge_sources: number;
  provenance_status: 'verified' | 'partial' | 'stale';
}

export interface ResourceBudget {
  max_cpu_pct: number;
  max_ram_mb: number;
  max_gpu_vram_mb: number;
  max_tokens_per_day: number;
  daily_cost_limit_cents: number;
  max_concurrency: number;
  schedule_cron: string;
  heartbeat_timeout_sec: number;
}

export interface AgentTelemetry {
  cpu_pct: number | null;
  ram_mb: number | null;
  gpu_vram_mb: number | null;
  active_workers: number;
  queue_depth: number;
  tokens_today: number;
  cost_today_cents: number;
  quota_remaining_pct: number;
  success_rate_pct: number;
  quality_score_pct: number;
  tasks_completed: number;
  tasks_failed: number;
  avg_latency_ms: number;
  sla_compliance_pct: number;
  retries_today: number;
  incidents_open: number;
  last_successful_run_at: string | null;
  last_error: string | null;
  last_heartbeat: string | null;
  telemetry_available: boolean;
}

export interface CanonicalAgent {
  agent_id: string;
  legacy_bot_id: string;
  tenant_id: string;
  workspace: string;
  display_name: string;
  role: string;
  description: string;
  version: number;
  config_version: string;
  owner_id: string;
  owner_name: string;
  team_id: string;
  team_name: string;
  assigned_to: string;
  status: AgentStatus;
  health: AgentHealth;
  current_task_id: string | null;
  current_task_title: string | null;
  parent_goal: string | null;
  project: string;
  priority: 'low' | 'normal' | 'high' | 'critical';
  progress_pct: number;
  next_action: string;
  blocker: string | null;
  task_age_minutes: number;
  handoff_to_agent_id: string | null;
  provider: string;
  model: string;
  endpoint_region: string;
  reasoning_mode: 'fast' | 'balanced' | 'deep';
  context_used: number;
  context_limit: number;
  fallback_model: string;
  skills: SkillBinding[];
  tools: ToolBinding[];
  connected_integrations: string[];
  memory_policy: MemoryPolicy;
  resource_budget: ResourceBudget;
  telemetry: AgentTelemetry;
  risk_level: RiskLevel;
  pending_approvals: number;
  data_sensitivity: 'public' | 'internal' | 'confidential' | 'restricted';
  audit_status: 'compliant' | 'review_required' | 'violation';
  policy_violations: number;
  permissions_summary: string;
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskRecord {
  task_id: string;
  tenant_id: string;
  workspace: string;
  title: string;
  objective: string;
  parent_goal: string;
  project: string;
  agent_id: string | null;
  owner_id: string;
  priority: 'low' | 'normal' | 'high' | 'critical';
  status: TaskStatus;
  progress_pct: number;
  inputs_summary: string;
  outputs_summary: string | null;
  checkpoint_ref: string | null;
  external_side_effects: 'none' | 'committed' | 'pending_reconciliation';
  evidence: Array<{
    evidence_id: string;
    label: string;
    source_uri: string;
    verified_at: string;
  }>;
  acceptance_checks: Array<{
    check: string;
    passed: boolean;
  }>;
  depends_on_task_ids: string[];
  handoff_to_agent_id: string | null;
  handoff_label: string | null;
  blocker_reason: string | null;
  attempts: number;
  correlation_id: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryItem {
  memory_id: string;
  tenant_id: string;
  agent_id: string;
  layer: 'raw_evidence' | 'working' | 'episodic' | 'semantic' | 'procedural' | 'decision';
  title: string;
  content: string;
  source: string;
  author: string;
  confidence: number;
  sensitivity: 'public' | 'internal' | 'confidential' | 'restricted';
  retention_until: string;
  version: number;
  status: 'active' | 'superseded' | 'disputed';
  provenance_type: 'source_fact' | 'agent_inference';
  citations: string[];
  competing_claim?: string;
  correlation_id: string;
  created_at: string;
}

export interface ActivityEvent {
  activity_id: string;
  tenant_id: string;
  agent_id: string;
  task_id?: string;
  actor_id: string;
  actor_name: string;
  action: string;
  target: string;
  result: 'success' | 'warning' | 'error' | 'pending';
  detail: string;
  correlation_id: string;
  occurred_at: string;
}

export interface IncidentRecord {
  incident_id: string;
  tenant_id: string;
  agent_id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  status: 'open' | 'investigating' | 'mitigated' | 'resolved';
  title: string;
  root_cause: string;
  impact: string;
  fix_summary: string | null;
  regression_test: string | null;
  rollback_point: string;
  correlation_id: string;
  opened_at: string;
  resolved_at: string | null;
}

export interface ApprovalRecord {
  approval_id: string;
  tenant_id: string;
  agent_id: string;
  task_id?: string;
  action_requested: string;
  reason: string;
  risk_level: RiskLevel;
  status: 'pending' | 'approved' | 'rejected';
  requested_by: string;
  decided_by: string | null;
  decision_note: string | null;
  correlation_id: string;
  created_at: string;
  decided_at: string | null;
}

export interface QualityEvaluation {
  eval_id: string;
  agent_id: string;
  suite: string;
  score_pct: number;
  passed: number;
  failed: number;
  human_corrections: number;
  acceptance_rate_pct: number;
  evaluated_at: string;
}

export interface AgentPerformancePoint {
  date: string;
  label: string;
  efficiency_pct: number;
  avg_latency_ms: number;
  tasks_completed: number;
  tasks_failed: number;
}

export type PerformanceTrendStatus = 'healthy' | 'degraded' | 'latency_high' | 'critical' | 'stale';

export interface AgentTrend30dBadge {
  code: PerformanceTrendStatus;
  label: string;
  reason: string;
}

export interface AgentTrend30dSummary {
  status: PerformanceTrendStatus;
  badges: AgentTrend30dBadge[];
  avg_efficiency_pct: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  efficiency_delta_7d: number;
  latency_delta_7d_ms: number;
  window_days: number;
}

export interface AuditEvent {
  audit_id: string;
  tenant_id: string;
  actor_id: string;
  actor_name: string;
  actor_role: ActorRole;
  action: string;
  entity_type: 'agent' | 'task' | 'approval' | 'integration' | 'graph' | 'incident' | 'configuration';
  entity_id: string;
  reason: string;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  approval_id: string | null;
  result: 'success' | 'rejected' | 'conflict' | 'error';
  correlation_id: string;
  occurred_at: string;
}

export interface IntegrationConnector {
  integration_id: string;
  tenant_id: string;
  name: string;
  provider: string;
  category: 'workspace' | 'messaging' | 'engineering' | 'runtime';
  status: 'healthy' | 'degraded' | 'api_issue' | 'offline';
  scopes: string[];
  secret_ref: string;
  last_success_at: string | null;
  last_error: string | null;
  rate_limit_remaining_pct: number;
  webhook_verified: boolean;
  sync_runs: number;
  dead_letter_count: number;
  connected_agents: string[];
}

export interface GraphifySnapshot {
  graph_version: number;
  repository: string;
  branch: string;
  commit_sha: string;
  generated_at: string;
  node_count: number;
  edge_count: number;
  stale: boolean;
  last_error: string | null;
  modules: Array<{ name: string; nodes: number; edges: number; entropy: number }>;
}

export interface GraphRefreshJob {
  job_id: string;
  tenant_id: string;
  status: 'queued' | 'running' | 'validating' | 'promoted' | 'failed' | 'cancelled';
  progress_pct: number;
  started_at: string;
  completed_at: string | null;
  target_version: number;
  error: string | null;
  correlation_id: string;
}

export interface WorkforceSummary {
  total_workforce: number;
  active: number;
  available: number;
  blocked: number;
  unhealthy: number;
  awaiting_approval: number;
  tasks_completed: number;
  tasks_failed: number;
  spend_today_cents: number;
  spend_today_formatted: string;
  daily_budget_cents: number;
  currency: 'USD';
  spend_state: 'estimated' | 'reconciled';
  resource_pressure: 'Normal' | 'Elevated' | 'High' | 'Stale';
  resource_pressure_agents: number;
  sla_compliance_pct: number;
  data_as_of: string;
}

export interface LiveStreamEvent {
  event_id: string;
  event_type:
    | 'agent.updated'
    | 'agent.heartbeat'
    | 'task.updated'
    | 'task.handoff'
    | 'approval.updated'
    | 'incident.opened'
    | 'usage.recorded'
    | 'graph.promoted';
  tenant_id: string;
  entity_id: string;
  entity_version: number;
  occurred_at: string;
  correlation_id: string;
  payload?: Record<string, unknown>;
}
