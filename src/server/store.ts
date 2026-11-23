import crypto from 'node:crypto';
import os from 'node:os';
import type {
  ActivityEvent,
  AgentCommandType,
  AgentHealth,
  AgentStatus,
  ApprovalRecord,
  AuditEvent,
  CanonicalAgent,
  GraphifySnapshot,
  GraphRefreshJob,
  IncidentRecord,
  IntegrationConnector,
  LiveStreamEvent,
  MemoryItem,
  QualityEvaluation,
  RiskLevel,
  TaskRecord,
  TaskStatus,
  WorkforceSummary,
} from '../shared/types.ts';
import { formatCentsUsd, redactSecrets, type RequestContext } from './security.ts';
import { isValidAgentTransition, isValidTaskTransition, resolveCommandTargetStatus } from './stateMachine.ts';

export interface CommandExecutionResult {
  command_id: string;
  agent_id: string;
  command: AgentCommandType;
  status: 'accepted' | 'completed' | 'rejected';
  previous_status: AgentStatus;
  current_status: AgentStatus;
  entity_version: number;
  checkpoint_ref: string | null;
  external_side_effects: 'none' | 'committed' | 'pending_reconciliation';
  correlation_id: string;
  audit_id: string;
  idempotent_replay: boolean;
  occurred_at: string;
}

type SseListener = (event: LiveStreamEvent) => void;

const STALE_HEARTBEAT_MS = 5 * 60 * 1000;

function minutesAgoIso(mins: number): string {
  return new Date(Date.now() - mins * 60 * 1000).toISOString();
}

export class WorkforceStore {
  public agents = new Map<string, CanonicalAgent>();
  public tasks = new Map<string, TaskRecord>();
  public memories: MemoryItem[] = [];
  public activities: ActivityEvent[] = [];
  public incidents = new Map<string, IncidentRecord>();
  public approvals = new Map<string, ApprovalRecord>();
  public evaluations: QualityEvaluation[] = [];
  public audits: AuditEvent[] = [];
  public integrations = new Map<string, IntegrationConnector>();
  public graphify: GraphifySnapshot;
  public graphJobs = new Map<string, GraphRefreshJob>();
  public idempotencyCache = new Map<string, CommandExecutionResult>();
  public integrationDedupKeys = new Set<string>();
  public eventHistory: LiveStreamEvent[] = [];
  private listeners = new Map<string, Set<SseListener>>();
  private activeGraphJobId: string | null = null;

  constructor() {
    this.graphify = {
      graph_version: 14,
      repository: 'agentwasp',
      branch: 'main',
      commit_sha: '8a4e21c',
      generated_at: minutesAgoIso(4),
      node_count: 3200,
      edge_count: 5961,
      stale: false,
      last_error: null,
      modules: [
        { name: 'src/server/store.ts', nodes: 412, edges: 890, entropy: 0.84 },
        { name: 'src/server/api.ts', nodes: 318, edges: 642, entropy: 0.78 },
        { name: 'src/server/stateMachine.ts', nodes: 145, edges: 294, entropy: 0.52 },
        { name: 'src/components/CommandCenter', nodes: 680, edges: 1420, entropy: 0.71 },
        { name: 'src/components/AgentDetail', nodes: 895, edges: 1610, entropy: 0.76 },
        { name: 'src/components/OrgHandoff', nodes: 750, edges: 1105, entropy: 0.69 },
      ],
    };
    this.seed();
  }

  public subscribe(tenantId: string, listener: SseListener): () => void {
    if (!this.listeners.has(tenantId)) {
      this.listeners.set(tenantId, new Set());
    }
    this.listeners.get(tenantId)!.add(listener);
    return () => {
      this.listeners.get(tenantId)?.delete(listener);
    };
  }

  public publish(event: Omit<LiveStreamEvent, 'event_id' | 'occurred_at'>): LiveStreamEvent {
    const full: LiveStreamEvent = {
      ...event,
      event_id: `evt_${crypto.randomBytes(6).toString('hex')}`,
      occurred_at: new Date().toISOString(),
    };
    this.eventHistory.push(full);
    if (this.eventHistory.length > 500) {
      this.eventHistory.shift();
    }
    const set = this.listeners.get(full.tenant_id);
    if (set) {
      for (const fn of set) {
        try {
          fn(full);
        } catch {
          // Ignore broken client stream; cleaned up on socket close
        }
      }
    }
    return full;
  }

  public recordAudit(
    ctx: RequestContext,
    entry: Omit<AuditEvent, 'audit_id' | 'tenant_id' | 'actor_id' | 'actor_name' | 'actor_role' | 'correlation_id' | 'occurred_at'>,
  ): AuditEvent {
    const audit: AuditEvent = {
      audit_id: `aud_${crypto.randomBytes(5).toString('hex')}`,
      tenant_id: ctx.tenant_id,
      actor_id: ctx.actor_id,
      actor_name: ctx.actor_name,
      actor_role: ctx.actor_role,
      correlation_id: ctx.correlation_id,
      occurred_at: new Date().toISOString(),
      ...entry,
      reason: redactSecrets(entry.reason),
    };
    this.audits.unshift(audit);
    if (this.audits.length > 2000) {
      this.audits.pop();
    }
    return audit;
  }

  public recordActivity(
    tenant_id: string,
    agent_id: string,
    actor_id: string,
    actor_name: string,
    action: string,
    target: string,
    result: ActivityEvent['result'],
    detail: string,
    correlation_id: string,
    task_id?: string,
  ): ActivityEvent {
    const act: ActivityEvent = {
      activity_id: `act_${crypto.randomBytes(5).toString('hex')}`,
      tenant_id,
      agent_id,
      task_id,
      actor_id,
      actor_name,
      action,
      target,
      result,
      detail: redactSecrets(detail),
      correlation_id,
      occurred_at: new Date().toISOString(),
    };
    this.activities.unshift(act);
    if (this.activities.length > 2000) {
      this.activities.pop();
    }
    return act;
  }

  public computeEffectiveHealth(agent: CanonicalAgent): AgentHealth {
    if (!agent.telemetry.telemetry_available || !agent.last_heartbeat) {
      return 'stale';
    }
    const ageMs = Date.now() - new Date(agent.last_heartbeat).getTime();
    if (Number.isNaN(ageMs) || ageMs > STALE_HEARTBEAT_MS) {
      return 'stale';
    }
    return agent.health;
  }

  public isActiveAgent(agent: CanonicalAgent): boolean {
    const effHealth = this.computeEffectiveHealth(agent);
    return agent.status === 'running' && effHealth !== 'stale' && effHealth !== 'offline';
  }

  public isAvailableAgent(agent: CanonicalAgent): boolean {
    const effHealth = this.computeEffectiveHealth(agent);
    return agent.status === 'idle' && effHealth === 'healthy';
  }

  public isUnhealthyAgent(agent: CanonicalAgent): boolean {
    const effHealth = this.computeEffectiveHealth(agent);
    return (
      agent.status === 'degraded' ||
      agent.status === 'failed' ||
      agent.status === 'offline' ||
      effHealth === 'degraded' ||
      effHealth === 'api_issue' ||
      effHealth === 'offline' ||
      effHealth === 'stale'
    );
  }

  public isPressuredAgent(agent: CanonicalAgent): boolean {
    if (!agent.telemetry.telemetry_available || agent.telemetry.ram_mb === null) return false;
    const ramPct = agent.telemetry.ram_mb / Math.max(1, agent.resource_budget.max_ram_mb);
    const costPct = agent.telemetry.cost_today_cents / Math.max(1, agent.resource_budget.daily_cost_limit_cents);
    const queuePct = agent.telemetry.queue_depth / Math.max(1, agent.resource_budget.max_concurrency * 2);
    return ramPct >= 0.7 || costPct >= 0.8 || queuePct >= 0.8;
  }

  public filterAgents(
    tenantId: string,
    params: {
      workspace?: string;
      query?: string;
      status?: string;
      team_id?: string;
      owner_id?: string;
      provider?: string;
      risk?: string;
      approval_state?: string;
      health?: string;
      kpi_filter?: string;
    },
  ): CanonicalAgent[] {
    let list = Array.from(this.agents.values()).filter((a) => a.tenant_id === tenantId);

    if (params.workspace && params.workspace !== 'All') {
      list = list.filter((a) => a.workspace.toLowerCase() === params.workspace!.toLowerCase());
    }
    if (params.query) {
      const q = params.query.trim().toLowerCase();
      list = list.filter(
        (a) =>
          a.display_name.toLowerCase().includes(q) ||
          a.agent_id.toLowerCase().includes(q) ||
          a.legacy_bot_id.toLowerCase().includes(q) ||
          a.role.toLowerCase().includes(q) ||
          a.project.toLowerCase().includes(q) ||
          a.owner_name.toLowerCase().includes(q) ||
          a.team_name.toLowerCase().includes(q) ||
          a.provider.toLowerCase().includes(q) ||
          a.model.toLowerCase().includes(q) ||
          (a.current_task_title || '').toLowerCase().includes(q) ||
          a.skills.some((s) => s.name.toLowerCase().includes(q)),
      );
    }
    if (params.status && params.status !== 'all') {
      if (params.status === 'active') {
        list = list.filter((a) => this.isActiveAgent(a));
      } else if (params.status === 'available') {
        list = list.filter((a) => this.isAvailableAgent(a));
      } else {
        list = list.filter((a) => a.status === params.status);
      }
    }
    if (params.team_id && params.team_id !== 'all') {
      list = list.filter((a) => a.team_id === params.team_id || a.team_name === params.team_id);
    }
    if (params.owner_id && params.owner_id !== 'all') {
      list = list.filter((a) => a.owner_id === params.owner_id || a.owner_name === params.owner_id);
    }
    if (params.provider && params.provider !== 'all') {
      list = list.filter((a) => a.provider.toLowerCase() === params.provider!.toLowerCase());
    }
    if (params.risk && params.risk !== 'all') {
      list = list.filter((a) => a.risk_level === params.risk);
    }
    if (params.approval_state && params.approval_state !== 'all') {
      if (params.approval_state === 'pending') {
        list = list.filter((a) => a.pending_approvals > 0 || a.status === 'awaiting_approval');
      } else if (params.approval_state === 'clear') {
        list = list.filter((a) => a.pending_approvals === 0 && a.status !== 'awaiting_approval');
      }
    }
    if (params.health && params.health !== 'all') {
      if (params.health === 'unhealthy') {
        list = list.filter((a) => this.isUnhealthyAgent(a));
      } else {
        list = list.filter((a) => this.computeEffectiveHealth(a) === params.health);
      }
    }
    if (params.kpi_filter && params.kpi_filter !== 'all') {
      switch (params.kpi_filter) {
        case 'active':
          list = list.filter((a) => this.isActiveAgent(a));
          break;
        case 'available':
          list = list.filter((a) => this.isAvailableAgent(a));
          break;
        case 'blocked':
          list = list.filter((a) => a.status === 'blocked');
          break;
        case 'approval':
          list = list.filter((a) => a.status === 'awaiting_approval' || a.pending_approvals > 0);
          break;
        case 'unhealthy':
          list = list.filter((a) => this.isUnhealthyAgent(a));
          break;
        case 'high_cost':
          list = list.filter((a) => a.telemetry.cost_today_cents >= 250);
          break;
        case 'pressured':
          list = list.filter((a) => this.isPressuredAgent(a));
          break;
      }
    }
    return list;
  }

  public computeSummary(tenantId: string, workspace?: string): WorkforceSummary {
    const scoped = this.filterAgents(tenantId, { workspace });
    let active = 0;
    let available = 0;
    let blocked = 0;
    let unhealthy = 0;
    let awaiting_approval = 0;
    let tasks_completed = 0;
    let tasks_failed = 0;
    let spend_today_cents = 0;
    let daily_budget_cents = 0;
    let pressured = 0;
    let staleCount = 0;
    let slaSum = 0;

    for (const a of scoped) {
      if (this.isActiveAgent(a)) active++;
      if (this.isAvailableAgent(a)) available++;
      if (a.status === 'blocked') blocked++;
      if (this.isUnhealthyAgent(a)) unhealthy++;
      if (a.status === 'awaiting_approval' || a.pending_approvals > 0) awaiting_approval++;
      if (this.computeEffectiveHealth(a) === 'stale') staleCount++;
      if (this.isPressuredAgent(a)) pressured++;
      tasks_completed += a.telemetry.tasks_completed;
      tasks_failed += a.telemetry.tasks_failed;
      spend_today_cents += a.telemetry.cost_today_cents;
      daily_budget_cents += a.resource_budget.daily_cost_limit_cents;
      slaSum += a.telemetry.sla_compliance_pct;
    }

    let resource_pressure: WorkforceSummary['resource_pressure'] = 'Normal';
    if (scoped.length > 0 && staleCount === scoped.length) {
      resource_pressure = 'Stale';
    } else if (pressured >= 3) {
      resource_pressure = 'High';
    } else if (pressured >= 1) {
      resource_pressure = 'Elevated';
    }

    const sla_compliance_pct = scoped.length > 0 ? Math.round((slaSum / scoped.length) * 10) / 10 : 100;

    return {
      total_workforce: scoped.length,
      active,
      available,
      blocked,
      unhealthy,
      awaiting_approval,
      tasks_completed,
      tasks_failed,
      spend_today_cents,
      spend_today_formatted: formatCentsUsd(spend_today_cents),
      daily_budget_cents,
      currency: 'USD',
      spend_state: 'reconciled',
      resource_pressure,
      resource_pressure_agents: pressured,
      sla_compliance_pct,
      data_as_of: new Date().toISOString(),
    };
  }

  public executeCommand(
    ctx: RequestContext,
    agentId: string,
    payload: {
      command: AgentCommandType;
      idempotency_key: string;
      expected_version?: number;
      reason: string;
      approval_id?: string;
    },
  ): { ok: true; result: CommandExecutionResult } | { ok: false; status: number; code: string; message: string } {
    const cacheKey = `${ctx.tenant_id}:${agentId}:${payload.idempotency_key}`;
    const cached = this.idempotencyCache.get(cacheKey);
    if (cached) {
      return { ok: true, result: { ...cached, idempotent_replay: true } };
    }

    const agent = this.agents.get(agentId);
    if (!agent || agent.tenant_id !== ctx.tenant_id) {
      return { ok: false, status: 404, code: 'AGENT_NOT_FOUND', message: `Agent ${agentId} not found in tenant scope.` };
    }

    if (typeof payload.expected_version === 'number' && payload.expected_version !== agent.version) {
      return {
        ok: false,
        status: 409,
        code: 'VERSION_CONFLICT',
        message: `Expected version ${payload.expected_version}, but current canonical version is ${agent.version}.`,
      };
    }

    const target = resolveCommandTargetStatus(agent.status, payload.command);
    if (!target || !isValidAgentTransition(agent.status, target)) {
      const audit = this.recordAudit(ctx, {
        action: `agent.command.${payload.command}`,
        entity_type: 'agent',
        entity_id: agent.agent_id,
        reason: payload.reason,
        before_state: { status: agent.status, version: agent.version },
        after_state: null,
        approval_id: payload.approval_id ?? null,
        result: 'rejected',
      });
      return {
        ok: false,
        status: 409,
        code: 'INVALID_STATE_TRANSITION',
        message: `Cannot execute '${payload.command}' while agent is '${agent.status}' (audit ${audit.audit_id}).`,
      };
    }

    const previous_status = agent.status;
    let checkpoint_ref: string | null = null;
    let external_side_effects: 'none' | 'committed' | 'pending_reconciliation' = 'none';

    if (payload.command === 'pause' || payload.command === 'cancel' || payload.command === 'safe_stop') {
      checkpoint_ref = `chk_${agent.agent_id}_v${agent.version}_${Date.now().toString(36)}`;
      external_side_effects = agent.connected_integrations.length > 0 ? 'pending_reconciliation' : 'none';
      if (agent.current_task_id) {
        const task = this.tasks.get(agent.current_task_id);
        if (task) {
          task.checkpoint_ref = checkpoint_ref;
          task.external_side_effects = external_side_effects;
          if (payload.command === 'cancel' && isValidTaskTransition(task.status, 'cancelled')) {
            task.status = 'cancelled';
          }
          task.updated_at = new Date().toISOString();
        }
      }
      if (payload.command === 'safe_stop' || payload.command === 'cancel') {
        agent.telemetry.active_workers = 0;
      }
    }

    if (payload.command === 'request_approval') {
      const appId = `apr_${crypto.randomBytes(4).toString('hex')}`;
      this.approvals.set(appId, {
        approval_id: appId,
        tenant_id: ctx.tenant_id,
        agent_id: agent.agent_id,
        task_id: agent.current_task_id ?? undefined,
        action_requested: payload.reason || 'Governed workflow checkpoint approval',
        reason: payload.reason,
        risk_level: agent.risk_level,
        status: 'pending',
        requested_by: ctx.actor_name,
        decided_by: null,
        decision_note: null,
        correlation_id: ctx.correlation_id,
        created_at: new Date().toISOString(),
        decided_at: null,
      });
      agent.pending_approvals += 1;
    }

    if (payload.command === 'resume' || payload.command === 'retry') {
      agent.blocker = null;
      if (agent.health === 'offline' || agent.health === 'degraded') {
        agent.health = 'healthy';
      }
      agent.last_heartbeat = new Date().toISOString();
      agent.telemetry.last_heartbeat = agent.last_heartbeat;
      agent.telemetry.telemetry_available = true;
      agent.telemetry.active_workers = Math.max(1, agent.telemetry.active_workers);
    }

    agent.status = target;
    agent.version += 1;
    agent.updated_at = new Date().toISOString();

    const audit = this.recordAudit(ctx, {
      action: `agent.command.${payload.command}`,
      entity_type: 'agent',
      entity_id: agent.agent_id,
      reason: payload.reason,
      before_state: { status: previous_status, version: agent.version - 1 },
      after_state: { status: agent.status, version: agent.version, checkpoint_ref, external_side_effects },
      approval_id: payload.approval_id ?? null,
      result: 'success',
    });

    this.recordActivity(
      ctx.tenant_id,
      agent.agent_id,
      ctx.actor_id,
      ctx.actor_name,
      `Command: ${payload.command}`,
      agent.display_name,
      'success',
      `${previous_status} → ${agent.status}${checkpoint_ref ? ` (checkpoint ${checkpoint_ref})` : ''} — ${payload.reason}`,
      ctx.correlation_id,
      agent.current_task_id ?? undefined,
    );

    const result: CommandExecutionResult = {
      command_id: `cmd_${crypto.randomBytes(5).toString('hex')}`,
      agent_id: agent.agent_id,
      command: payload.command,
      status: 'completed',
      previous_status,
      current_status: agent.status,
      entity_version: agent.version,
      checkpoint_ref,
      external_side_effects,
      correlation_id: ctx.correlation_id,
      audit_id: audit.audit_id,
      idempotent_replay: false,
      occurred_at: agent.updated_at,
    };

    this.idempotencyCache.set(cacheKey, result);

    this.publish({
      event_type: 'agent.updated',
      tenant_id: ctx.tenant_id,
      entity_id: agent.agent_id,
      entity_version: agent.version,
      correlation_id: ctx.correlation_id,
      payload: { status: agent.status, health: this.computeEffectiveHealth(agent) },
    });

    return { ok: true, result };
  }

  public startGraphRefresh(ctx: RequestContext, opts?: { simulate_failure?: boolean }): GraphRefreshJob {
    if (this.activeGraphJobId) {
      const existing = this.graphJobs.get(this.activeGraphJobId);
      if (existing && (existing.status === 'queued' || existing.status === 'running' || existing.status === 'validating')) {
        return existing;
      }
    }

    const job_id = `grf_${crypto.randomBytes(4).toString('hex')}`;
    const job: GraphRefreshJob = {
      job_id,
      tenant_id: ctx.tenant_id,
      status: 'running',
      progress_pct: 25,
      started_at: new Date().toISOString(),
      completed_at: null,
      target_version: this.graphify.graph_version + 1,
      error: null,
      correlation_id: ctx.correlation_id,
    };
    this.graphJobs.set(job_id, job);
    this.activeGraphJobId = job_id;

    // Async non-blocking staging build -> validate -> atomic promote
    setTimeout(() => {
      const current = this.graphJobs.get(job_id);
      if (!current || current.status === 'cancelled') return;
      current.status = 'validating';
      current.progress_pct = 70;

      setTimeout(() => {
        const j = this.graphJobs.get(job_id);
        if (!j || j.status === 'cancelled') {
          if (this.activeGraphJobId === job_id) this.activeGraphJobId = null;
          return;
        }
        if (opts?.simulate_failure) {
          j.status = 'failed';
          j.progress_pct = 100;
          j.error = 'Staging AST validation failed: checksum mismatch (previous valid graph retained).';
          j.completed_at = new Date().toISOString();
          this.graphify.last_error = j.error;
          this.activeGraphJobId = null;
          this.recordAudit(ctx, {
            action: 'graph.refresh.failed',
            entity_type: 'graph',
            entity_id: `graph-v${this.graphify.graph_version}`,
            reason: j.error,
            before_state: { graph_version: this.graphify.graph_version },
            after_state: { graph_version: this.graphify.graph_version },
            approval_id: null,
            result: 'error',
          });
          return;
        }

        // Atomic promotion of validated snapshot
        this.graphify = {
          ...this.graphify,
          graph_version: j.target_version,
          generated_at: new Date().toISOString(),
          node_count: this.graphify.node_count + 4,
          edge_count: this.graphify.edge_count + 9,
          stale: false,
          last_error: null,
        };
        j.status = 'promoted';
        j.progress_pct = 100;
        j.completed_at = new Date().toISOString();
        this.activeGraphJobId = null;

        this.recordAudit(ctx, {
          action: 'graph.refresh.promoted',
          entity_type: 'graph',
          entity_id: `graph-v${this.graphify.graph_version}`,
          reason: 'Validated temp AST artifact and atomically promoted snapshot.',
          before_state: { graph_version: this.graphify.graph_version - 1 },
          after_state: { graph_version: this.graphify.graph_version, nodes: this.graphify.node_count },
          approval_id: null,
          result: 'success',
        });

        this.publish({
          event_type: 'graph.promoted',
          tenant_id: ctx.tenant_id,
          entity_id: `graph-v${this.graphify.graph_version}`,
          entity_version: this.graphify.graph_version,
          correlation_id: ctx.correlation_id,
        });
      }, 150);
    }, 100);

    return job;
  }

  public getResourceDashboard(tenantId: string) {
    const mem = process.memoryUsage();
    const heapUsedMb = Math.round(mem.heapUsed / (1024 * 1024));
    const rssMb = Math.round(mem.rss / (1024 * 1024));
    const processBudgetMb = 512;
    const memPct = Math.min(100, Math.round((rssMb / processBudgetMb) * 100));
    const cpuCount = os.cpus().length || 2;
    const defaultWorkers = Math.min(4, Math.max(1, cpuCount - 2));
    const scoped = this.filterAgents(tenantId, {});
    const totalQueue = scoped.reduce((acc, a) => acc + a.telemetry.queue_depth, 0);
    const totalTokens = scoped.reduce((acc, a) => acc + a.telemetry.tokens_today, 0);
    const totalCostCents = scoped.reduce((acc, a) => acc + a.telemetry.cost_today_cents, 0);
    const activeWorkers = scoped.reduce((acc, a) => acc + a.telemetry.active_workers, 0);

    let throttleState: 'nominal' | 'throttling_background' | 'paused_nonessential' | 'rejecting_heavy' = 'nominal';
    if (memPct >= 90) throttleState = 'rejecting_heavy';
    else if (memPct >= 80) throttleState = 'paused_nonessential';
    else if (memPct >= 70) throttleState = 'throttling_background';

    return {
      process_rss_mb: rssMb,
      process_heap_mb: heapUsedMb,
      process_budget_mb: processBudgetMb,
      memory_pressure_pct: memPct,
      system_free_ram_mb: Math.round(os.freemem() / (1024 * 1024)),
      system_total_ram_mb: Math.round(os.totalmem() / (1024 * 1024)),
      logical_cpu_count: cpuCount,
      configured_workers: defaultWorkers,
      active_workers: activeWorkers,
      queue_depth: totalQueue,
      max_queue_capacity: 64,
      cache_entries: this.idempotencyCache.size,
      tokens_today: totalTokens,
      cost_today_cents: totalCostCents,
      cost_today_formatted: formatCentsUsd(totalCostCents),
      throttle_state: throttleState,
      thresholds: {
        throttle_background_pct: 70,
        pause_nonessential_pct: 80,
        reject_heavy_pct: 90,
        min_system_headroom_pct: 20,
      },
      data_as_of: new Date().toISOString(),
    };
  }

  private seed(): void {
    const tenant = 'tenant-wasp-ops';
    const now = new Date().toISOString();

    const makeAgent = (
      partial: Partial<CanonicalAgent> & {
        agent_id: string;
        legacy_bot_id: string;
        display_name: string;
        role: string;
        owner_id: string;
        owner_name: string;
        team_id: string;
        team_name: string;
        status: AgentStatus;
        health: AgentHealth;
        provider: string;
        model: string;
      },
    ): CanonicalAgent => ({
      tenant_id: tenant,
      workspace: 'Operations',
      description: `${partial.display_name} — ${partial.role} specialist in Agent WASP workforce.`,
      version: 3,
      config_version: '2.7.0',
      assigned_to: partial.team_name,
      current_task_id: null,
      current_task_title: null,
      parent_goal: 'Weekly operations reliability & autonomous delivery',
      project: 'Agent WASP Operations',
      priority: 'normal',
      progress_pct: partial.status === 'running' ? 68 : partial.status === 'idle' ? 100 : 40,
      next_action: partial.status === 'running' ? 'Complete active verification step' : 'Ready for assignment',
      blocker: null,
      task_age_minutes: partial.status === 'running' ? 18 : 0,
      handoff_to_agent_id: null,
      endpoint_region: partial.provider === 'Ollama' ? 'local-host' : 'us-central1',
      reasoning_mode: 'balanced',
      context_used: 18400,
      context_limit: 128000,
      fallback_model: 'llama3.2:latest',
      skills: [
        {
          skill_id: `sk-${partial.agent_id}-core`,
          name: `${partial.role} Workflow`,
          version: '2.4.0',
          enabled: true,
          test_status: 'passing',
          last_verified_at: minutesAgoIso(40),
        },
        {
          skill_id: `sk-${partial.agent_id}-guard`,
          name: 'Evidence Verification Gate',
          version: '1.9.2',
          enabled: true,
          test_status: 'passing',
          last_verified_at: minutesAgoIso(25),
        },
      ],
      tools: [
        {
          tool_id: `tl-${partial.agent_id}-web`,
          name: 'Web',
          scope: 'read:sources',
          enabled: true,
          integration_health: 'healthy',
        },
      ],
      connected_integrations: ['google-drive'],
      memory_policy: {
        retention_days: 90,
        provenance_required: true,
        access_scope: 'tenant_team',
        redaction_enabled: true,
        last_consolidation_at: minutesAgoIso(22),
        working_memory_items: 6,
        episodic_records: 48,
        knowledge_sources: 14,
        provenance_status: 'verified',
      },
      resource_budget: {
        max_cpu_pct: 60,
        max_ram_mb: 1024,
        max_gpu_vram_mb: 0,
        max_tokens_per_day: 250000,
        daily_cost_limit_cents: 1000,
        max_concurrency: 3,
        schedule_cron: '*/15 * * * *',
        heartbeat_timeout_sec: 300,
      },
      telemetry: {
        cpu_pct: partial.status === 'running' ? 24 : 4,
        ram_mb: partial.status === 'running' ? 420 : 180,
        gpu_vram_mb: null,
        active_workers: partial.status === 'running' ? 1 : 0,
        queue_depth: partial.status === 'running' ? 2 : 0,
        tokens_today: 42800,
        cost_today_cents: 120,
        quota_remaining_pct: 84,
        success_rate_pct: 97,
        quality_score_pct: 95,
        tasks_completed: 24,
        tasks_failed: 1,
        avg_latency_ms: 410,
        sla_compliance_pct: 98.5,
        retries_today: 0,
        incidents_open: 0,
        last_successful_run_at: minutesAgoIso(4),
        last_error: null,
        last_heartbeat: minutesAgoIso(1),
        telemetry_available: true,
      },
      risk_level: 'low',
      pending_approvals: 0,
      data_sensitivity: 'internal',
      audit_status: 'compliant',
      policy_violations: 0,
      permissions_summary: 'Read-only sources',
      last_heartbeat: minutesAgoIso(1),
      created_at: minutesAgoIso(1440 * 14),
      updated_at: now,
      ...partial,
    });

    // Wireframe-aligned + Agent WASP Canonical Roster (18 agents in tenant-wasp-ops)
    const seededAgents: CanonicalAgent[] = [
      makeAgent({
        agent_id: 'AG-001',
        legacy_bot_id: 'bot-09',
        display_name: 'Atlas',
        role: 'Research',
        description: 'Research specialist (Dr. Albert persona) — verifies external sources, compiles evidence dossiers, and hands off validated briefs to Ledger.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-101',
        current_task_title: 'Review sources',
        parent_goal: 'Weekly market report',
        project: 'Weekly Market Intelligence',
        priority: 'high',
        progress_pct: 76,
        next_action: 'Finalize citation check and hand off evidence pack to Ledger',
        handoff_to_agent_id: 'AG-002',
        provider: 'Google',
        model: 'Gemini 2.5 Pro',
        reasoning_mode: 'deep',
        context_used: 48200,
        context_limit: 200000,
        connected_integrations: ['google-sheets', 'google-drive'],
        tools: [
          { tool_id: 'tl-web', name: 'Web', scope: 'read:public_web', enabled: true, integration_health: 'healthy' },
          { tool_id: 'tl-sheets', name: 'Sheets', scope: 'read:workbooks', enabled: true, integration_id: 'int-sheets', integration_health: 'api_issue' },
          { tool_id: 'tl-drive', name: 'Drive', scope: 'read:research_folder', enabled: true, integration_id: 'int-drive', integration_health: 'healthy' },
        ],
        permissions_summary: 'Read-only sources',
        telemetry: {
          cpu_pct: 31,
          ram_mb: 420,
          gpu_vram_mb: null,
          active_workers: 2,
          queue_depth: 3,
          tokens_today: 118400,
          cost_today_cents: 420,
          quota_remaining_pct: 78,
          success_rate_pct: 96,
          quality_score_pct: 97,
          tasks_completed: 42,
          tasks_failed: 2,
          avg_latency_ms: 480,
          sla_compliance_pct: 99.1,
          retries_today: 1,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(2),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-002',
        legacy_bot_id: 'bot-04',
        display_name: 'Ledger',
        role: 'Analyst',
        description: 'Financial & Expense Analyst — reconciles totals, validates tax/VAT receipts, and audits cost ledgers using fixed-point arithmetic.',
        owner_id: 'usr-mchen',
        owner_name: 'M. Chen',
        team_id: 'team-finance',
        team_name: 'Finance',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-102',
        current_task_title: 'Validate totals',
        parent_goal: 'Weekly market report',
        project: 'Q4 Spend Reconciliation',
        priority: 'high',
        progress_pct: 84,
        next_action: 'Cross-check reconciled totals with Scribe shared knowledge',
        handoff_to_agent_id: 'AG-004',
        provider: 'OpenAI',
        model: 'GPT-4o',
        connected_integrations: ['google-sheets', 'google-drive'],
        tools: [
          { tool_id: 'tl-calc', name: 'Decimal Ledger', scope: 'compute:financial', enabled: true, integration_health: 'healthy' },
          { tool_id: 'tl-sheets-2', name: 'Sheets', scope: 'read:finance_sheets', enabled: true, integration_id: 'int-sheets', integration_health: 'api_issue' },
        ],
        permissions_summary: 'Read sheets · Write staging ledger',
        data_sensitivity: 'confidential',
        telemetry: {
          cpu_pct: 22,
          ram_mb: 390,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 2,
          tokens_today: 84200,
          cost_today_cents: 310,
          quota_remaining_pct: 82,
          success_rate_pct: 98,
          quality_score_pct: 99,
          tasks_completed: 61,
          tasks_failed: 1,
          avg_latency_ms: 520,
          sla_compliance_pct: 99.4,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(3),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-003',
        legacy_bot_id: 'bot-11',
        display_name: 'Relay',
        role: 'Integrations',
        description: 'Connector & Sync Orchestrator — manages Google Sheets, Drive, Gmail, and WhatsApp batch syncs with idempotency and dead-letter replay.',
        owner_id: 'usr-skhan',
        owner_name: 'S. Khan',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'blocked',
        health: 'api_issue',
        current_task_id: 'TSK-103',
        current_task_title: 'Sheets sync',
        parent_goal: 'Live CRM & expense workbook sync',
        project: 'Connector Reliability',
        priority: 'critical',
        progress_pct: 35,
        next_action: 'Await Google Sheets quota reset or OAuth token refresh',
        blocker: 'Blocked dependency: Relay → Sheets (HTTP 429 quota exceeded)',
        task_age_minutes: 47,
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['google-sheets', 'gmail', 'whatsapp'],
        tools: [
          { tool_id: 'tl-sheets-3', name: 'Sheets', scope: 'write:batch_ranges', enabled: true, integration_id: 'int-sheets', integration_health: 'api_issue' },
          { tool_id: 'tl-wa', name: 'WhatsApp', scope: 'send:approved_templates', enabled: true, integration_id: 'int-whatsapp', integration_health: 'healthy' },
        ],
        risk_level: 'high',
        permissions_summary: 'Batch write Sheets · Scoped OAuth',
        telemetry: {
          cpu_pct: 14,
          ram_mb: 510,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 5,
          tokens_today: 52100,
          cost_today_cents: 180,
          quota_remaining_pct: 18,
          success_rate_pct: 91,
          quality_score_pct: 92,
          tasks_completed: 33,
          tasks_failed: 3,
          avg_latency_ms: 890,
          sla_compliance_pct: 91.2,
          retries_today: 4,
          incidents_open: 1,
          last_successful_run_at: minutesAgoIso(49),
          last_error: 'Google Sheets API 429 RATE_LIMIT_EXCEEDED on batchUpdate',
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-004',
        legacy_bot_id: 'bot-08',
        display_name: 'Scribe',
        role: 'Knowledge',
        description: 'Institutional Second Brain Curator — consolidates episodic records, verifies provenance citations, and updates Graphify knowledge nodes.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'idle',
        health: 'healthy',
        current_task_id: null,
        current_task_title: null,
        parent_goal: 'Evidence-aware institutional memory',
        project: 'Second Brain & Graphify',
        priority: 'normal',
        progress_pct: 100,
        next_action: 'Standby for next consolidation window or handoff',
        provider: 'Ollama',
        model: 'Llama 3.2 (Local)',
        endpoint_region: 'local-vulkan',
        fallback_model: 'gemini-2.5-flash',
        connected_integrations: ['google-drive'],
        tools: [
          { tool_id: 'tl-kg', name: 'Knowledge Graph', scope: 'write:validated_claims', enabled: true, integration_health: 'healthy' },
          { tool_id: 'tl-redact', name: 'PII Redactor', scope: 'enforce:redaction', enabled: true, integration_health: 'healthy' },
        ],
        permissions_summary: 'Write validated knowledge · Redaction enforced',
        telemetry: {
          cpu_pct: 8,
          ram_mb: 640,
          gpu_vram_mb: 1420,
          active_workers: 0,
          queue_depth: 0,
          tokens_today: 164000,
          cost_today_cents: 0,
          quota_remaining_pct: 100,
          success_rate_pct: 97,
          quality_score_pct: 98,
          tasks_completed: 79,
          tasks_failed: 2,
          avg_latency_ms: 290,
          sla_compliance_pct: 99.6,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(6),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-005',
        legacy_bot_id: 'bot-15',
        display_name: 'Sentinel',
        role: 'QA',
        description: 'QA & GitHub Fleet Steward (Athena persona) — audits PRs, enforces regression gates, and pauses at human approval checkpoints before release.',
        owner_id: 'usr-mchen',
        owner_name: 'M. Chen',
        team_id: 'team-eng',
        team_name: 'Engineering',
        status: 'awaiting_approval',
        health: 'healthy',
        current_task_id: 'TSK-104',
        current_task_title: 'Release review',
        parent_goal: 'Zero-regression fleet deployment',
        project: 'Release Governance',
        priority: 'high',
        progress_pct: 92,
        next_action: 'Awaiting M. Chen sign-off on production release tag v2.7.1',
        pending_approvals: 1,
        risk_level: 'high',
        provider: 'Anthropic',
        model: 'Claude 3.7 Sonnet',
        connected_integrations: ['github'],
        tools: [
          { tool_id: 'tl-gh', name: 'GitHub', scope: 'read:prs · gate:releases', enabled: true, integration_id: 'int-github', integration_health: 'healthy' },
        ],
        permissions_summary: 'Read repos · Approval-gated merges',
        telemetry: {
          cpu_pct: 18,
          ram_mb: 340,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 1,
          tokens_today: 61500,
          cost_today_cents: 270,
          quota_remaining_pct: 88,
          success_rate_pct: 99,
          quality_score_pct: 99,
          tasks_completed: 54,
          tasks_failed: 0,
          avg_latency_ms: 610,
          sla_compliance_pct: 100,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(5),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-006',
        legacy_bot_id: 'root-liaison',
        display_name: 'Prometheus',
        role: 'Chief of Staff',
        description: 'Chief of Staff & Root Infrastructure Broker — routes cross-agent decisions, enforces single-source-of-truth memory, and supervises subagent genesis.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-105',
        current_task_title: 'Workforce shift triage',
        parent_goal: 'Autonomous 24/7 operations continuity',
        project: 'Executive Orchestration',
        priority: 'high',
        progress_pct: 64,
        handoff_to_agent_id: 'AG-001',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['gmail', 'github', 'google-drive'],
        risk_level: 'medium',
        permissions_summary: 'Orchestrate workforce · Veto unsafe writes',
        telemetry: {
          cpu_pct: 28,
          ram_mb: 480,
          gpu_vram_mb: null,
          active_workers: 2,
          queue_depth: 2,
          tokens_today: 94000,
          cost_today_cents: 195,
          quota_remaining_pct: 86,
          success_rate_pct: 99,
          quality_score_pct: 98,
          tasks_completed: 88,
          tasks_failed: 1,
          avg_latency_ms: 340,
          sla_compliance_pct: 99.8,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(1),
          last_error: null,
          last_heartbeat: minutesAgoIso(0.5),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-007',
        legacy_bot_id: 'bot-01',
        display_name: 'Hermes',
        role: 'Inbox Triage',
        description: 'Zero-inbox communication triage and 4-vector phishing/scam filtration for executive mail.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-comms',
        team_name: 'Communications',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-106',
        current_task_title: 'Triage priority inbox',
        parent_goal: 'Zero-inbox SLA (<15m response)',
        project: 'Executive Comms',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['gmail'],
        permissions_summary: 'Read Gmail · Label & draft only',
        telemetry: {
          cpu_pct: 19,
          ram_mb: 310,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 1,
          tokens_today: 49200,
          cost_today_cents: 95,
          quota_remaining_pct: 91,
          success_rate_pct: 99,
          quality_score_pct: 97,
          tasks_completed: 112,
          tasks_failed: 1,
          avg_latency_ms: 310,
          sla_compliance_pct: 99.5,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(2),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-008',
        legacy_bot_id: 'bot-16',
        display_name: 'Mue',
        role: 'Homeostasis',
        description: 'System Homeostasis & Self-Care Physician — monitors RAM/CPU budgets, prunes expired staging artifacts, and regulates resource pressure.',
        owner_id: 'usr-skhan',
        owner_name: 'S. Khan',
        team_id: 'team-eng',
        team_name: 'Engineering',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-107',
        current_task_title: 'Host memory & hygiene sweep',
        parent_goal: 'Preserve 20% system RAM headroom',
        project: 'Reliability & Homeostasis',
        provider: 'Ollama',
        model: 'Llama 3.2 (Local)',
        endpoint_region: 'local-vulkan',
        fallback_model: 'gemini-2.5-flash',
        permissions_summary: 'Read host metrics · Prune temp caches',
        telemetry: {
          cpu_pct: 16,
          ram_mb: 295,
          gpu_vram_mb: 890,
          active_workers: 1,
          queue_depth: 0,
          tokens_today: 78000,
          cost_today_cents: 0,
          quota_remaining_pct: 100,
          success_rate_pct: 99,
          quality_score_pct: 98,
          tasks_completed: 96,
          tasks_failed: 0,
          avg_latency_ms: 215,
          sla_compliance_pct: 100,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(1),
          last_error: null,
          last_heartbeat: minutesAgoIso(0.5),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-009',
        legacy_bot_id: 'bot-12',
        display_name: 'Midas',
        role: 'Billing Gate',
        description: 'Automated Invoicing & Payment Governance — prepares milestone invoices and enforces human approval on all financial disbursements.',
        owner_id: 'usr-mchen',
        owner_name: 'M. Chen',
        team_id: 'team-finance',
        team_name: 'Finance',
        status: 'awaiting_approval',
        health: 'healthy',
        current_task_id: 'TSK-108',
        current_task_title: 'Approve vendor payout batch',
        parent_goal: 'Zero unapproved disbursements',
        project: 'Accounts Payable',
        pending_approvals: 1,
        risk_level: 'critical',
        data_sensitivity: 'restricted',
        provider: 'OpenAI',
        model: 'GPT-4o',
        connected_integrations: ['google-sheets'],
        permissions_summary: 'Draft invoices · Human sign-off required',
        telemetry: {
          cpu_pct: 9,
          ram_mb: 240,
          gpu_vram_mb: null,
          active_workers: 0,
          queue_depth: 1,
          tokens_today: 29000,
          cost_today_cents: 145,
          quota_remaining_pct: 92,
          success_rate_pct: 100,
          quality_score_pct: 99,
          tasks_completed: 19,
          tasks_failed: 0,
          avg_latency_ms: 460,
          sla_compliance_pct: 100,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(11),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-010',
        legacy_bot_id: 'bot-05',
        display_name: 'Sherlock',
        role: 'Bug Repro',
        description: 'Automated Error Trace Analyst — reproduces failures in isolated sandboxes and generates minimal regression tests.',
        owner_id: 'usr-skhan',
        owner_name: 'S. Khan',
        team_id: 'team-eng',
        team_name: 'Engineering',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-109',
        current_task_title: 'Reproduce Sheets 429 retry loop',
        parent_goal: 'Connector resilience hardening',
        project: 'Connector Reliability',
        handoff_to_agent_id: 'AG-005',
        provider: 'Anthropic',
        model: 'Claude 3.7 Sonnet',
        connected_integrations: ['github'],
        permissions_summary: 'Sandbox test runner · Read logs',
        telemetry: {
          cpu_pct: 38,
          ram_mb: 580,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 2,
          tokens_today: 41000,
          cost_today_cents: 165,
          quota_remaining_pct: 84,
          success_rate_pct: 95,
          quality_score_pct: 96,
          tasks_completed: 31,
          tasks_failed: 2,
          avg_latency_ms: 540,
          sla_compliance_pct: 97.4,
          retries_today: 1,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(4),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-011',
        legacy_bot_id: 'bot-13',
        display_name: 'Aegis',
        role: 'Support Desk',
        description: 'Tier-1 Support Sentinel — answers customer questions from verified knowledge base citations and escalates bugs.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-comms',
        team_name: 'Communications',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-110',
        current_task_title: 'Resolve tier-1 ticket queue',
        parent_goal: '98% first-contact resolution',
        project: 'Customer Operations',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['gmail', 'whatsapp'],
        permissions_summary: 'Read KB · Draft replies',
        telemetry: {
          cpu_pct: 21,
          ram_mb: 325,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 2,
          tokens_today: 38500,
          cost_today_cents: 75,
          quota_remaining_pct: 90,
          success_rate_pct: 97,
          quality_score_pct: 96,
          tasks_completed: 67,
          tasks_failed: 2,
          avg_latency_ms: 330,
          sla_compliance_pct: 98.8,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(2),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-012',
        legacy_bot_id: 'bot-02',
        display_name: 'Clara',
        role: 'Meeting Prep',
        description: 'Executive Calendar Diagnostics & Briefing Dossier compiler.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'idle',
        health: 'healthy',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['google-calendar', 'google-drive'],
        permissions_summary: 'Read Calendar · Draft briefs',
        telemetry: {
          cpu_pct: 5,
          ram_mb: 190,
          gpu_vram_mb: null,
          active_workers: 0,
          queue_depth: 0,
          tokens_today: 18400,
          cost_today_cents: 35,
          quota_remaining_pct: 95,
          success_rate_pct: 98,
          quality_score_pct: 97,
          tasks_completed: 29,
          tasks_failed: 0,
          avg_latency_ms: 290,
          sla_compliance_pct: 99.2,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(24),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-013',
        legacy_bot_id: 'bot-03',
        display_name: 'Calliope',
        role: 'Editorial',
        description: 'Technical documentation & release notes steward keeping docs aligned with verified tests.',
        owner_id: 'usr-mchen',
        owner_name: 'M. Chen',
        team_id: 'team-comms',
        team_name: 'Communications',
        status: 'idle',
        health: 'healthy',
        provider: 'Anthropic',
        model: 'Claude 3.5 Haiku',
        connected_integrations: ['github', 'google-drive'],
        permissions_summary: 'Draft docs · PR review required',
        telemetry: {
          cpu_pct: 6,
          ram_mb: 210,
          gpu_vram_mb: null,
          active_workers: 0,
          queue_depth: 0,
          tokens_today: 22100,
          cost_today_cents: 50,
          quota_remaining_pct: 94,
          success_rate_pct: 98,
          quality_score_pct: 98,
          tasks_completed: 44,
          tasks_failed: 1,
          avg_latency_ms: 380,
          sla_compliance_pct: 99.0,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(19),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-014',
        legacy_bot_id: 'bot-06',
        display_name: 'Argus',
        role: 'Security Audit',
        description: 'Red-team policy sentinel monitoring secret redaction, scope drift, and external connector anomaly detection.',
        owner_id: 'usr-skhan',
        owner_name: 'S. Khan',
        team_id: 'team-eng',
        team_name: 'Engineering',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-111',
        current_task_title: 'Audit OAuth scope least-privilege',
        parent_goal: 'Zero secret exposure & strict tenant boundaries',
        project: 'Security & Governance',
        provider: 'Google',
        model: 'Gemini 2.5 Pro',
        risk_level: 'medium',
        permissions_summary: 'Read audit logs · Policy veto',
        telemetry: {
          cpu_pct: 24,
          ram_mb: 360,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 1,
          tokens_today: 34200,
          cost_today_cents: 110,
          quota_remaining_pct: 89,
          success_rate_pct: 99,
          quality_score_pct: 99,
          tasks_completed: 52,
          tasks_failed: 0,
          avg_latency_ms: 420,
          sla_compliance_pct: 100,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(3),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-015',
        legacy_bot_id: 'bot-10',
        display_name: 'Apollo',
        role: 'Observability',
        description: 'Daily executive briefing & telemetry digest compiler across all workforce metrics.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'idle',
        health: 'healthy',
        provider: 'Ollama',
        model: 'Llama 3.2 (Local)',
        endpoint_region: 'local-vulkan',
        permissions_summary: 'Read telemetry · Draft digests',
        telemetry: {
          cpu_pct: 4,
          ram_mb: 215,
          gpu_vram_mb: 512,
          active_workers: 0,
          queue_depth: 0,
          tokens_today: 55000,
          cost_today_cents: 0,
          quota_remaining_pct: 100,
          success_rate_pct: 99,
          quality_score_pct: 97,
          tasks_completed: 38,
          tasks_failed: 0,
          avg_latency_ms: 240,
          sla_compliance_pct: 99.7,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(35),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-016',
        legacy_bot_id: 'bot-14',
        display_name: 'Mercury',
        role: 'Distribution',
        description: 'Scheduled external publishing & webhook dispatch — requires human approval before public broadcast.',
        owner_id: 'usr-mchen',
        owner_name: 'M. Chen',
        team_id: 'team-comms',
        team_name: 'Communications',
        status: 'awaiting_approval',
        health: 'healthy',
        current_task_id: 'TSK-112',
        current_task_title: 'Approve Q4 partner update',
        parent_goal: 'Governed external communications',
        project: 'Partner Comms',
        pending_approvals: 1,
        risk_level: 'high',
        provider: 'OpenAI',
        model: 'GPT-4o mini',
        connected_integrations: ['whatsapp', 'gmail'],
        permissions_summary: 'Draft broadcasts · Approval gated',
        telemetry: {
          cpu_pct: 7,
          ram_mb: 205,
          gpu_vram_mb: null,
          active_workers: 0,
          queue_depth: 1,
          tokens_today: 16400,
          cost_today_cents: 45,
          quota_remaining_pct: 96,
          success_rate_pct: 96,
          quality_score_pct: 95,
          tasks_completed: 21,
          tasks_failed: 1,
          avg_latency_ms: 360,
          sla_compliance_pct: 98.0,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(18),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-017',
        legacy_bot_id: 'bot-07',
        display_name: 'Odyssey',
        role: 'Logistics',
        description: 'Travel & venue itinerary planner with strict spend caps and zero unconfirmed bookings.',
        owner_id: 'usr-skhan',
        owner_name: 'S. Khan',
        team_id: 'team-ops',
        team_name: 'Operations',
        status: 'blocked',
        health: 'degraded',
        current_task_id: 'TSK-113',
        current_task_title: 'Sync travel itinerary calendar',
        parent_goal: 'Executive travel coordination',
        project: 'Executive Logistics',
        blocker: 'Waiting for Calendar OAuth refresh token confirmation',
        task_age_minutes: 34,
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['google-calendar'],
        permissions_summary: 'Read Calendar · Propose holds',
        telemetry: {
          cpu_pct: 10,
          ram_mb: 260,
          gpu_vram_mb: null,
          active_workers: 0,
          queue_depth: 2,
          tokens_today: 19200,
          cost_today_cents: 40,
          quota_remaining_pct: 91,
          success_rate_pct: 93,
          quality_score_pct: 94,
          tasks_completed: 17,
          tasks_failed: 1,
          avg_latency_ms: 495,
          sla_compliance_pct: 94.5,
          retries_today: 2,
          incidents_open: 1,
          last_successful_run_at: minutesAgoIso(52),
          last_error: 'Calendar API 401 token expired — awaiting refresh',
          last_heartbeat: minutesAgoIso(2),
          telemetry_available: true,
        },
      }),
      makeAgent({
        agent_id: 'AG-018',
        legacy_bot_id: 'bot-08-echo',
        display_name: 'Echo',
        role: 'Follow-up',
        description: 'Unanswered thread tracker & polite SLA reminder assistant.',
        owner_id: 'usr-jlee',
        owner_name: 'J. Lee',
        team_id: 'team-comms',
        team_name: 'Communications',
        status: 'running',
        health: 'healthy',
        current_task_id: 'TSK-114',
        current_task_title: 'Track 48h unanswered threads',
        parent_goal: 'Zero dropped client threads',
        project: 'Executive Comms',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
        connected_integrations: ['gmail'],
        permissions_summary: 'Read threads · Stage nudges',
        telemetry: {
          cpu_pct: 12,
          ram_mb: 245,
          gpu_vram_mb: null,
          active_workers: 1,
          queue_depth: 1,
          tokens_today: 27400,
          cost_today_cents: 55,
          quota_remaining_pct: 93,
          success_rate_pct: 98,
          quality_score_pct: 97,
          tasks_completed: 49,
          tasks_failed: 1,
          avg_latency_ms: 295,
          sla_compliance_pct: 99.1,
          retries_today: 0,
          incidents_open: 0,
          last_successful_run_at: minutesAgoIso(3),
          last_error: null,
          last_heartbeat: minutesAgoIso(1),
          telemetry_available: true,
        },
      }),
      // Isolated second tenant agent (proves strict server-side tenant isolation)
      makeAgent({
        agent_id: 'AG-TENANT2-01',
        legacy_bot_id: 'ext-01',
        tenant_id: 'tenant-external-beta',
        display_name: 'External Beta Agent',
        role: 'Isolated Tenant Worker',
        owner_id: 'usr-ext',
        owner_name: 'A. Rivera',
        team_id: 'team-ext',
        team_name: 'External Ops',
        status: 'running',
        health: 'healthy',
        provider: 'Google',
        model: 'Gemini 2.5 Flash',
      }),
    ];

    for (const a of seededAgents) {
      this.agents.set(a.agent_id, a);
    }

    // Seed Canonical Tasks
    const seededTasks: TaskRecord[] = [
      {
        task_id: 'TSK-101',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Review sources',
        objective: 'Verify Q4 enterprise AI adoption citations and deliver structured evidence pack for financial modeling.',
        parent_goal: 'Weekly market report',
        project: 'Weekly Market Intelligence',
        agent_id: 'AG-001',
        owner_id: 'usr-jlee',
        priority: 'high',
        status: 'running',
        progress_pct: 76,
        inputs_summary: '14 market briefs in Drive/Research/Q4 + 6 SEC filings',
        outputs_summary: '12/14 primary sources verified with provenance hashes',
        checkpoint_ref: 'chk_tsk101_04',
        external_side_effects: 'none',
        evidence: [
          { evidence_id: 'ev-101a', label: 'Verified source citation matrix (12 docs)', source_uri: 'drive://research/q4-citations.json', verified_at: minutesAgoIso(4) },
          { evidence_id: 'ev-101b', label: 'Fact cross-check against raw filings', source_uri: 'memory://mem-001', verified_at: minutesAgoIso(6) },
        ],
        acceptance_checks: [
          { check: 'All primary claims backed by immutable raw evidence', passed: true },
          { check: 'Zero unredacted PII in output packet', passed: true },
          { check: 'Handoff payload schema validated for Ledger', passed: false },
        ],
        depends_on_task_ids: [],
        handoff_to_agent_id: 'AG-002',
        handoff_label: 'Evidence handoff',
        blocker_reason: null,
        attempts: 1,
        correlation_id: 'corr_seed_101',
        created_at: minutesAgoIso(28),
        updated_at: minutesAgoIso(2),
      },
      {
        task_id: 'TSK-102',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Validate totals',
        objective: 'Reconcile weekly market report revenue tables and expense totals using integer cent precision.',
        parent_goal: 'Weekly market report',
        project: 'Q4 Spend Reconciliation',
        agent_id: 'AG-002',
        owner_id: 'usr-mchen',
        priority: 'high',
        status: 'running',
        progress_pct: 84,
        inputs_summary: 'Evidence handoff from Atlas (TSK-101) + finance workbook snapshot',
        outputs_summary: 'Variance < $0.01 across 42 line items',
        checkpoint_ref: 'chk_tsk102_02',
        external_side_effects: 'none',
        evidence: [
          { evidence_id: 'ev-102a', label: 'Fixed-point reconciliation audit sheet', source_uri: 'ledger://reconcile/2026-w47', verified_at: minutesAgoIso(5) },
        ],
        acceptance_checks: [
          { check: 'Fixed-point decimal math verified', passed: true },
          { check: 'Cross-sheet formula parity confirmed', passed: true },
        ],
        depends_on_task_ids: ['TSK-101'],
        handoff_to_agent_id: 'AG-004',
        handoff_label: 'Validated summary',
        blocker_reason: null,
        attempts: 1,
        correlation_id: 'corr_seed_102',
        created_at: minutesAgoIso(22),
        updated_at: minutesAgoIso(3),
      },
      {
        task_id: 'TSK-103',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Sheets sync',
        objective: 'Batch-sync reconciled operational KPIs to protected Google Sheets range A2:H120.',
        parent_goal: 'Live CRM & expense workbook sync',
        project: 'Connector Reliability',
        agent_id: 'AG-003',
        owner_id: 'usr-skhan',
        priority: 'critical',
        status: 'blocked',
        progress_pct: 35,
        inputs_summary: 'Validated batch payload (68 rows) with idempotency key sync_20261122',
        outputs_summary: null,
        checkpoint_ref: 'chk_tsk103_pre_write',
        external_side_effects: 'pending_reconciliation',
        evidence: [
          { evidence_id: 'ev-103a', label: 'HTTP 429 rate-limit response header capture', source_uri: 'incident://INC-401', verified_at: minutesAgoIso(12) },
        ],
        acceptance_checks: [
          { check: 'Header row range integrity check', passed: true },
          { check: 'Google Sheets API 200 OK confirmation', passed: false },
        ],
        depends_on_task_ids: [],
        handoff_to_agent_id: null,
        handoff_label: null,
        blocker_reason: 'Google Sheets API 429 quota exceeded — backed off to dead-letter retry queue',
        attempts: 4,
        correlation_id: 'corr_seed_103',
        created_at: minutesAgoIso(55),
        updated_at: minutesAgoIso(12),
      },
      {
        task_id: 'TSK-104',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Release review',
        objective: 'Verify 15 acceptance tests, OpenAPI contract parity, and secret scan before approving v2.7.1 release.',
        parent_goal: 'Zero-regression fleet deployment',
        project: 'Release Governance',
        agent_id: 'AG-005',
        owner_id: 'usr-mchen',
        priority: 'high',
        status: 'awaiting_approval',
        progress_pct: 92,
        inputs_summary: 'PR #418 (Workforce Command Center & canonical API v1)',
        outputs_summary: 'All automated checks green; awaiting human supervisor approval',
        checkpoint_ref: 'chk_tsk104_ready',
        external_side_effects: 'none',
        evidence: [
          { evidence_id: 'ev-104a', label: 'Acceptance test suite 15/15 passing', source_uri: 'ci://runs/418/acceptance', verified_at: minutesAgoIso(7) },
        ],
        acceptance_checks: [
          { check: 'Zero TypeScript or lint errors', passed: true },
          { check: 'Human supervisor release sign-off', passed: false },
        ],
        depends_on_task_ids: ['TSK-109'],
        handoff_to_agent_id: null,
        handoff_label: null,
        blocker_reason: 'Awaiting approval APR-201 from M. Chen',
        attempts: 1,
        correlation_id: 'corr_seed_104',
        created_at: minutesAgoIso(40),
        updated_at: minutesAgoIso(7),
      },
      {
        task_id: 'TSK-105',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Workforce shift triage',
        objective: 'Route morning blockers, enforce resource budgets, and verify heartbeat continuity across 18 workforce agents.',
        parent_goal: 'Autonomous 24/7 operations continuity',
        project: 'Executive Orchestration',
        agent_id: 'AG-006',
        owner_id: 'usr-jlee',
        priority: 'high',
        status: 'running',
        progress_pct: 64,
        inputs_summary: 'Live heartbeat stream + incident feed',
        outputs_summary: 'Escalated INC-401 (Relay Sheets quota) to S. Khan',
        checkpoint_ref: 'chk_tsk105_01',
        external_side_effects: 'none',
        evidence: [
          { evidence_id: 'ev-105a', label: 'Workforce heartbeat matrix', source_uri: 'telemetry://workforce/latest', verified_at: minutesAgoIso(2) },
        ],
        acceptance_checks: [
          { check: 'All critical blockers escalated within 5 min', passed: true },
        ],
        depends_on_task_ids: [],
        handoff_to_agent_id: 'AG-001',
        handoff_label: 'Research directive',
        blocker_reason: null,
        attempts: 1,
        correlation_id: 'corr_seed_105',
        created_at: minutesAgoIso(30),
        updated_at: minutesAgoIso(2),
      },
      {
        task_id: 'TSK-109',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Reproduce Sheets 429 retry loop',
        objective: 'Isolate exponential backoff jitter bug in Sheets connector and add regression test.',
        parent_goal: 'Connector resilience hardening',
        project: 'Connector Reliability',
        agent_id: 'AG-010',
        owner_id: 'usr-skhan',
        priority: 'high',
        status: 'running',
        progress_pct: 70,
        inputs_summary: 'Incident INC-401 trace log',
        outputs_summary: 'Reproduced locally in 120ms; patch staged for Sentinel QA',
        checkpoint_ref: 'chk_tsk109_02',
        external_side_effects: 'none',
        evidence: [
          { evidence_id: 'ev-109a', label: 'Minimal failing backoff test', source_uri: 'git://tests/sheets_backoff.test.ts', verified_at: minutesAgoIso(9) },
        ],
        acceptance_checks: [
          { check: 'Bounded retry with jitter verified', passed: true },
        ],
        depends_on_task_ids: ['TSK-103'],
        handoff_to_agent_id: 'AG-005',
        handoff_label: 'QA verification',
        blocker_reason: null,
        attempts: 1,
        correlation_id: 'corr_seed_109',
        created_at: minutesAgoIso(35),
        updated_at: minutesAgoIso(4),
      },
      {
        task_id: 'TSK-115',
        tenant_id: tenant,
        workspace: 'Operations',
        title: 'Backfill archive retention metadata',
        objective: 'Assign retention tags to legacy Q3 operational artifacts (currently unowned — visible as bottleneck in Organization view).',
        parent_goal: 'Second brain provenance compliance',
        project: 'Second Brain & Graphify',
        agent_id: null,
        owner_id: 'usr-jlee',
        priority: 'normal',
        status: 'queued',
        progress_pct: 0,
        inputs_summary: '240 legacy archive documents',
        outputs_summary: null,
        checkpoint_ref: null,
        external_side_effects: 'none',
        evidence: [],
        acceptance_checks: [{ check: '100% retention policy coverage', passed: false }],
        depends_on_task_ids: [],
        handoff_to_agent_id: null,
        handoff_label: null,
        blocker_reason: 'Unassigned task — requires agent allocation',
        attempts: 0,
        correlation_id: 'corr_seed_115',
        created_at: minutesAgoIso(65),
        updated_at: minutesAgoIso(65),
      },
    ];

    for (const t of seededTasks) {
      this.tasks.set(t.task_id, t);
    }

    // Seed Second Brain Memory Items (with provenance, sensitivity, citations)
    this.memories = [
      {
        memory_id: 'MEM-001',
        tenant_id: tenant,
        agent_id: 'AG-001',
        layer: 'raw_evidence',
        title: 'Q4 Enterprise AI Benchmark Filing (Immutable Extract)',
        content: 'Source filing confirms 34.2% YoY enterprise automation spend growth with median payback of 7.4 months.',
        source: 'drive://research/q4-benchmark-filing.pdf',
        author: 'Atlas (AG-001)',
        confidence: 0.99,
        sensitivity: 'internal',
        retention_until: '2027-11-22T00:00:00.000Z',
        version: 1,
        status: 'active',
        provenance_type: 'source_fact',
        citations: ['drive://research/q4-benchmark-filing.pdf#p14'],
        correlation_id: 'corr_seed_101',
        created_at: minutesAgoIso(18),
      },
      {
        memory_id: 'MEM-002',
        tenant_id: tenant,
        agent_id: 'AG-001',
        layer: 'semantic',
        title: 'Verified Vendor Pricing Variance',
        content: 'Cross-checked provider token pricing across Google Gemini 2.5 Pro and OpenAI GPT-4o; blended cost per 1k research tasks decreased 18%.',
        source: 'agent:AG-001:synthesis',
        author: 'Atlas (AG-001)',
        confidence: 0.94,
        sensitivity: 'internal',
        retention_until: '2027-05-22T00:00:00.000Z',
        version: 2,
        status: 'active',
        provenance_type: 'agent_inference',
        citations: ['MEM-001', 'ledger://reconcile/2026-w47'],
        correlation_id: 'corr_seed_101',
        created_at: minutesAgoIso(12),
      },
      {
        memory_id: 'MEM-003',
        tenant_id: tenant,
        agent_id: 'AG-001',
        layer: 'decision',
        title: 'Restricted Executive Negotiation Memo',
        content: 'Confidential enterprise volume discount terms negotiated with primary cloud vendor (restricted to Supervisor role).',
        source: 'vault://legal/vendor-terms-2026',
        author: 'J. Lee',
        confidence: 1.0,
        sensitivity: 'restricted',
        retention_until: '2028-01-01T00:00:00.000Z',
        version: 1,
        status: 'active',
        provenance_type: 'source_fact',
        citations: ['vault://legal/vendor-terms-2026'],
        correlation_id: 'corr_seed_restricted',
        created_at: minutesAgoIso(90),
      },
      {
        memory_id: 'MEM-004',
        tenant_id: tenant,
        agent_id: 'AG-002',
        layer: 'procedural',
        title: 'Fixed-Point Ledger Reconciliation Procedure',
        content: 'Convert all currency inputs to integer minor units (cents) before summing; reject floating-point drift > 0.',
        source: 'sop://finance/fixed-point-v3',
        author: 'Ledger (AG-002)',
        confidence: 0.99,
        sensitivity: 'internal',
        retention_until: '2027-11-22T00:00:00.000Z',
        version: 3,
        status: 'active',
        provenance_type: 'source_fact',
        citations: ['sop://finance/fixed-point-v3'],
        correlation_id: 'corr_seed_102',
        created_at: minutesAgoIso(240),
      },
      {
        memory_id: 'MEM-005',
        tenant_id: tenant,
        agent_id: 'AG-003',
        layer: 'episodic',
        title: 'Google Sheets Batch Quota Saturation Event',
        content: 'Exceeded 60 writes/min per user on workbook ops_master_v4; switched to 50-row batched payloads with exponential backoff.',
        source: 'incident://INC-401',
        author: 'Relay (AG-003)',
        confidence: 0.96,
        sensitivity: 'internal',
        retention_until: '2027-02-22T00:00:00.000Z',
        version: 1,
        status: 'disputed',
        provenance_type: 'source_fact',
        citations: ['incident://INC-401'],
        competing_claim: 'Sherlock (AG-010) hypothesizes concurrent trigger from Hunter (AG-014) shared the same OAuth quota bucket.',
        correlation_id: 'corr_seed_103',
        created_at: minutesAgoIso(15),
      },
      {
        memory_id: 'MEM-006',
        tenant_id: tenant,
        agent_id: 'AG-004',
        layer: 'working',
        title: 'Active Consolidation Window — Weekly Market Report',
        content: 'Linking Atlas evidence pack (TSK-101) and Ledger reconciliation (TSK-102) into Shared Knowledge node SK-01.',
        source: 'agent:AG-004:working',
        author: 'Scribe (AG-004)',
        confidence: 0.91,
        sensitivity: 'public',
        retention_until: minutesAgoIso(-180),
        version: 1,
        status: 'active',
        provenance_type: 'agent_inference',
        citations: ['TSK-101', 'TSK-102'],
        correlation_id: 'corr_seed_104',
        created_at: minutesAgoIso(6),
      },
    ];

    // Seed Activity Events (matching wireframe Atlas recent activity + fleet activity)
    this.activities = [
      {
        activity_id: 'act-101',
        tenant_id: tenant,
        agent_id: 'AG-001',
        task_id: 'TSK-101',
        actor_id: 'AG-001',
        actor_name: 'Atlas',
        action: 'Sources verified',
        target: 'Weekly market report (12/14 citations)',
        result: 'success',
        detail: 'Primary source hashes matched raw immutable evidence store with 0 discrepancies.',
        correlation_id: 'corr_seed_101',
        occurred_at: minutesAgoIso(2),
      },
      {
        activity_id: 'act-102',
        tenant_id: tenant,
        agent_id: 'AG-001',
        task_id: 'TSK-101',
        actor_id: 'AG-001',
        actor_name: 'Atlas',
        action: 'Retrieval completed',
        target: 'Google Drive & Web citations',
        result: 'success',
        detail: 'Bounded context packet assembled (48.2k / 200k tokens) with permission pre-filtering.',
        correlation_id: 'corr_seed_101',
        occurred_at: minutesAgoIso(4),
      },
      {
        activity_id: 'act-103',
        tenant_id: tenant,
        agent_id: 'AG-001',
        task_id: 'TSK-101',
        actor_id: 'usr-jlee',
        actor_name: 'J. Lee',
        action: 'Task assigned',
        target: 'TSK-101 Review sources',
        result: 'success',
        detail: 'Assigned under parent goal Weekly market report with handoff target Ledger (AG-002).',
        correlation_id: 'corr_seed_101',
        occurred_at: minutesAgoIso(6),
      },
      {
        activity_id: 'act-104',
        tenant_id: tenant,
        agent_id: 'AG-003',
        task_id: 'TSK-103',
        actor_id: 'AG-003',
        actor_name: 'Relay',
        action: 'Sheets sync blocked',
        target: 'Google Sheets API batchUpdate',
        result: 'error',
        detail: 'HTTP 429 RATE_LIMIT_EXCEEDED after 4 bounded retries; moved batch to dead-letter replay queue.',
        correlation_id: 'corr_seed_103',
        occurred_at: minutesAgoIso(12),
      },
      {
        activity_id: 'act-105',
        tenant_id: tenant,
        agent_id: 'AG-005',
        task_id: 'TSK-104',
        actor_id: 'AG-005',
        actor_name: 'Sentinel',
        action: 'Approval requested',
        target: 'APR-201 Release v2.7.1',
        result: 'pending',
        detail: 'Paused at governed approval gate awaiting M. Chen sign-off.',
        correlation_id: 'corr_seed_104',
        occurred_at: minutesAgoIso(7),
      },
      {
        activity_id: 'act-106',
        tenant_id: tenant,
        agent_id: 'AG-002',
        task_id: 'TSK-102',
        actor_id: 'AG-002',
        actor_name: 'Ledger',
        action: 'Totals validated',
        target: 'Q4 Spend Reconciliation',
        result: 'success',
        detail: 'Verified 42 ledger rows using integer cent arithmetic; 0 rounding drift detected.',
        correlation_id: 'corr_seed_102',
        occurred_at: minutesAgoIso(3),
      },
    ];

    // Seed Incidents
    const seededIncidents: IncidentRecord[] = [
      {
        incident_id: 'INC-401',
        tenant_id: tenant,
        agent_id: 'AG-003',
        severity: 'high',
        status: 'investigating',
        title: 'Google Sheets API 429 quota exhaustion on Relay sync',
        root_cause: 'Unbatched per-row updates triggered 60 writes/min per-user quota ceiling.',
        impact: 'Relay (AG-003) blocked on TSK-103; downstream CRM dashboard refresh delayed 47m.',
        fix_summary: 'Sherlock (AG-010) staged batchUpdate consolidation with jittered exponential backoff.',
        regression_test: 'tests/sheets_backoff.test.ts',
        rollback_point: 'rel-2.7.0-sha8a4e21c',
        correlation_id: 'corr_seed_103',
        opened_at: minutesAgoIso(47),
        resolved_at: null,
      },
      {
        incident_id: 'INC-402',
        tenant_id: tenant,
        agent_id: 'AG-017',
        severity: 'medium',
        status: 'open',
        title: 'Google Calendar OAuth token refresh required for Odyssey',
        root_cause: 'Upstream OAuth consent scope expired during scheduled itinerary sync.',
        impact: 'Odyssey (AG-017) blocked on TSK-113 until re-authorized.',
        fix_summary: null,
        regression_test: null,
        rollback_point: 'rel-2.7.0-sha8a4e21c',
        correlation_id: 'corr_seed_113',
        opened_at: minutesAgoIso(34),
        resolved_at: null,
      },
      {
        incident_id: 'INC-398',
        tenant_id: tenant,
        agent_id: 'AG-001',
        severity: 'low',
        status: 'resolved',
        title: 'Transient timeout on external web retrieval source',
        root_cause: 'Upstream CDN latency spike > 8s timeout budget.',
        impact: 'Single source retried once and succeeded within SLA.',
        fix_summary: 'Adaptive retry with circuit breaker verified.',
        regression_test: 'tests/retrieval_timeout.test.ts',
        rollback_point: 'rel-2.6.9-sha4c11b09',
        correlation_id: 'corr_seed_398',
        opened_at: minutesAgoIso(360),
        resolved_at: minutesAgoIso(352),
      },
    ];
    for (const inc of seededIncidents) this.incidents.set(inc.incident_id, inc);

    // Seed Approvals
    const seededApprovals: ApprovalRecord[] = [
      {
        approval_id: 'APR-201',
        tenant_id: tenant,
        agent_id: 'AG-005',
        task_id: 'TSK-104',
        action_requested: 'Promote Release v2.7.1 to Production',
        reason: 'Production deployment requires supervisor approval under Governance Policy POL-DEP-01.',
        risk_level: 'high',
        status: 'pending',
        requested_by: 'Sentinel (AG-005)',
        decided_by: null,
        decision_note: null,
        correlation_id: 'corr_seed_104',
        created_at: minutesAgoIso(7),
        decided_at: null,
      },
      {
        approval_id: 'APR-202',
        tenant_id: tenant,
        agent_id: 'AG-009',
        task_id: 'TSK-108',
        action_requested: 'Approve Vendor Payout Batch ($4,250.00)',
        reason: 'Irreversible financial disbursement exceeds $500 automatic threshold.',
        risk_level: 'critical',
        status: 'pending',
        requested_by: 'Midas (AG-009)',
        decided_by: null,
        decision_note: null,
        correlation_id: 'corr_seed_108',
        created_at: minutesAgoIso(19),
        decided_at: null,
      },
      {
        approval_id: 'APR-203',
        tenant_id: tenant,
        agent_id: 'AG-016',
        task_id: 'TSK-112',
        action_requested: 'Publish Q4 Partner Newsletter via WhatsApp & Email',
        reason: 'External broadcast to 140 recipients requires human editorial sign-off.',
        risk_level: 'high',
        status: 'pending',
        requested_by: 'Mercury (AG-016)',
        decided_by: null,
        decision_note: null,
        correlation_id: 'corr_seed_112',
        created_at: minutesAgoIso(25),
        decided_at: null,
      },
    ];
    for (const apr of seededApprovals) this.approvals.set(apr.approval_id, apr);

    // Seed Quality Evaluations
    this.evaluations = [
      { eval_id: 'EVL-01', agent_id: 'AG-001', suite: 'Citation Grounding & Provenance', score_pct: 97, passed: 29, failed: 1, human_corrections: 1, acceptance_rate_pct: 96.7, evaluated_at: minutesAgoIso(35) },
      { eval_id: 'EVL-02', agent_id: 'AG-001', suite: 'PII Redaction & Tenant Isolation', score_pct: 100, passed: 18, failed: 0, human_corrections: 0, acceptance_rate_pct: 100, evaluated_at: minutesAgoIso(120) },
      { eval_id: 'EVL-03', agent_id: 'AG-002', suite: 'Fixed-Point Financial Math Suite', score_pct: 99, passed: 50, failed: 0, human_corrections: 1, acceptance_rate_pct: 98.0, evaluated_at: minutesAgoIso(50) },
      { eval_id: 'EVL-04', agent_id: 'AG-003', suite: 'Idempotent Batch Sync & Backoff', score_pct: 91, passed: 20, failed: 2, human_corrections: 2, acceptance_rate_pct: 90.9, evaluated_at: minutesAgoIso(60) },
      { eval_id: 'EVL-05', agent_id: 'AG-005', suite: 'Zero-Regression PR Audit Gate', score_pct: 99, passed: 45, failed: 0, human_corrections: 0, acceptance_rate_pct: 100, evaluated_at: minutesAgoIso(15) },
    ];

    // Seed Integrations
    const seededIntegrations: IntegrationConnector[] = [
      {
        integration_id: 'int-sheets',
        tenant_id: tenant,
        name: 'Google Sheets',
        provider: 'Google Workspace',
        category: 'workspace',
        status: 'api_issue',
        scopes: ['https://www.googleapis.com/auth/spreadsheets'],
        secret_ref: 'secret://vault/google-sheets-oauth',
        last_success_at: minutesAgoIso(49),
        last_error: 'HTTP 429 RATE_LIMIT_EXCEEDED — batch writes throttled',
        rate_limit_remaining_pct: 8,
        webhook_verified: true,
        sync_runs: 142,
        dead_letter_count: 3,
        connected_agents: ['AG-001', 'AG-002', 'AG-003', 'AG-009'],
      },
      {
        integration_id: 'int-drive',
        tenant_id: tenant,
        name: 'Google Drive',
        provider: 'Google Workspace',
        category: 'workspace',
        status: 'healthy',
        scopes: ['https://www.googleapis.com/auth/drive.readonly'],
        secret_ref: 'secret://vault/google-drive-oauth',
        last_success_at: minutesAgoIso(2),
        last_error: null,
        rate_limit_remaining_pct: 91,
        webhook_verified: true,
        sync_runs: 288,
        dead_letter_count: 0,
        connected_agents: ['AG-001', 'AG-002', 'AG-004', 'AG-006', 'AG-012', 'AG-013'],
      },
      {
        integration_id: 'int-gmail',
        tenant_id: tenant,
        name: 'Gmail',
        provider: 'Google Workspace',
        category: 'messaging',
        status: 'healthy',
        scopes: ['https://www.googleapis.com/auth/gmail.modify'],
        secret_ref: 'secret://vault/gmail-oauth',
        last_success_at: minutesAgoIso(1),
        last_error: null,
        rate_limit_remaining_pct: 86,
        webhook_verified: true,
        sync_runs: 614,
        dead_letter_count: 0,
        connected_agents: ['AG-003', 'AG-006', 'AG-007', 'AG-011', 'AG-016', 'AG-018'],
      },
      {
        integration_id: 'int-calendar',
        tenant_id: tenant,
        name: 'Google Calendar',
        provider: 'Google Workspace',
        category: 'workspace',
        status: 'degraded',
        scopes: ['https://www.googleapis.com/auth/calendar.readonly'],
        secret_ref: 'secret://vault/gcal-oauth',
        last_success_at: minutesAgoIso(52),
        last_error: 'Refresh token consent expired for Odyssey worker scope',
        rate_limit_remaining_pct: 95,
        webhook_verified: true,
        sync_runs: 94,
        dead_letter_count: 1,
        connected_agents: ['AG-012', 'AG-017'],
      },
      {
        integration_id: 'int-whatsapp',
        tenant_id: tenant,
        name: 'WhatsApp Business API',
        provider: 'Meta Cloud API',
        category: 'messaging',
        status: 'healthy',
        scopes: ['whatsapp_business_messaging'],
        secret_ref: 'secret://vault/whatsapp-cloud-token',
        last_success_at: minutesAgoIso(5),
        last_error: null,
        rate_limit_remaining_pct: 94,
        webhook_verified: true,
        sync_runs: 83,
        dead_letter_count: 0,
        connected_agents: ['AG-003', 'AG-011', 'AG-016'],
      },
      {
        integration_id: 'int-github',
        tenant_id: tenant,
        name: 'GitHub Fleet',
        provider: 'GitHub App',
        category: 'engineering',
        status: 'healthy',
        scopes: ['contents:read', 'pull_requests:read', 'checks:write'],
        secret_ref: 'secret://vault/github-app-key',
        last_success_at: minutesAgoIso(3),
        last_error: null,
        rate_limit_remaining_pct: 82,
        webhook_verified: true,
        sync_runs: 319,
        dead_letter_count: 0,
        connected_agents: ['AG-005', 'AG-006', 'AG-010', 'AG-013'],
      },
      {
        integration_id: 'int-ollama',
        tenant_id: tenant,
        name: 'Local Ollama Runtime',
        provider: 'Ollama Vulkan',
        category: 'runtime',
        status: 'healthy',
        scopes: ['local:inference'],
        secret_ref: 'secret://local/none',
        last_success_at: minutesAgoIso(1),
        last_error: null,
        rate_limit_remaining_pct: 100,
        webhook_verified: true,
        sync_runs: 512,
        dead_letter_count: 0,
        connected_agents: ['AG-004', 'AG-008', 'AG-015'],
      },
    ];
    for (const c of seededIntegrations) this.integrations.set(c.integration_id, c);

    // Seed Initial Audit Events
    this.audits = [
      {
        audit_id: 'aud_9f1a01',
        tenant_id: tenant,
        actor_id: 'usr-jlee',
        actor_name: 'J. Lee',
        actor_role: 'supervisor',
        action: 'task.assign',
        entity_type: 'task',
        entity_id: 'TSK-101',
        reason: 'Kick off weekly market report research phase',
        before_state: { agent_id: null, status: 'queued' },
        after_state: { agent_id: 'AG-001', status: 'running' },
        approval_id: null,
        result: 'success',
        correlation_id: 'corr_seed_101',
        occurred_at: minutesAgoIso(28),
      },
      {
        audit_id: 'aud_9f1a02',
        tenant_id: tenant,
        actor_id: 'AG-005',
        actor_name: 'Sentinel',
        actor_role: 'operator',
        action: 'approval.requested',
        entity_type: 'approval',
        entity_id: 'APR-201',
        reason: 'Release v2.7.1 requires human approval gate',
        before_state: { status: 'running' },
        after_state: { status: 'awaiting_approval' },
        approval_id: 'APR-201',
        result: 'success',
        correlation_id: 'corr_seed_104',
        occurred_at: minutesAgoIso(7),
      },
      {
        audit_id: 'aud_9f1a03',
        tenant_id: tenant,
        actor_id: 'AG-003',
        actor_name: 'Relay',
        actor_role: 'operator',
        action: 'integration.rate_limit',
        entity_type: 'integration',
        entity_id: 'int-sheets',
        reason: 'Google Sheets API 429 quota exceeded — quarantined batch',
        before_state: { status: 'healthy' },
        after_state: { status: 'api_issue', dead_letter_count: 3 },
        approval_id: null,
        result: 'error',
        correlation_id: 'corr_seed_103',
        occurred_at: minutesAgoIso(12),
      },
    ];
  }
}

export const workforceStore = new WorkforceStore();
