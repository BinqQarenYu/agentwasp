import crypto from 'node:crypto';
import express, { type Request, type Response } from 'express';
import type {
  AgentCommandType,
  CanonicalAgent,
  RiskLevel,
  TaskRecord,
  TaskStatus,
} from '../shared/types.ts';
import { extractContext, formatCentsUsd, hasPermission, redactSecrets } from './security.ts';
import { isValidTaskTransition } from './stateMachine.ts';
import { WorkforceStore, workforceStore } from './store.ts';

function sendError(
  res: Response,
  status: number,
  code: string,
  message: string,
  correlationId: string,
  retryable = false,
  details?: Record<string, unknown>,
) {
  return res.status(status).json({
    error: {
      code,
      message,
      correlation_id: correlationId,
      retryable,
      ...(details ? { details } : {}),
    },
  });
}

function escapeCsv(value: unknown): string {
  const s = String(value ?? '');
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

export function createApiRouter(store: WorkforceStore = workforceStore) {
  const router = express.Router();

  // ── 1. Workforce Command Center ───────────────────────────────────────────

  router.get('/workforce/summary', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const workspace = typeof req.query.workspace === 'string' ? req.query.workspace : undefined;
    const summary = store.computeSummary(ctx.tenant_id, workspace);
    res.json({ ...summary, correlation_id: ctx.correlation_id });
  });

  router.get('/agents', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const workspace = typeof req.query.workspace === 'string' ? req.query.workspace : undefined;
    const query = typeof req.query.query === 'string' ? req.query.query : undefined;
    const status = typeof req.query.status === 'string' ? req.query.status : undefined;
    const team_id = typeof req.query.team_id === 'string' ? req.query.team_id : undefined;
    const owner_id = typeof req.query.owner_id === 'string' ? req.query.owner_id : undefined;
    const provider = typeof req.query.provider === 'string' ? req.query.provider : undefined;
    const risk = typeof req.query.risk === 'string' ? req.query.risk : undefined;
    const approval_state = typeof req.query.approval_state === 'string' ? req.query.approval_state : undefined;
    const health = typeof req.query.health === 'string' ? req.query.health : undefined;
    const kpi_filter = typeof req.query.kpi_filter === 'string' ? req.query.kpi_filter : undefined;
    const sort = typeof req.query.sort === 'string' ? req.query.sort : 'agent_id';
    const sort_dir = req.query.sort_dir === 'desc' ? 'desc' : 'asc';
    const group_by = typeof req.query.group_by === 'string' ? req.query.group_by : 'none';
    const page = Math.max(1, Number.parseInt(String(req.query.page || '1'), 10) || 1);
    const page_size = Math.min(100, Math.max(1, Number.parseInt(String(req.query.page_size || '10'), 10) || 10));

    const filtered = store.filterAgents(ctx.tenant_id, {
      workspace,
      query,
      status,
      team_id,
      owner_id,
      provider,
      risk,
      approval_state,
      health,
      kpi_filter,
    });

    // Multi-field deterministic server-side sort
    const sorted = [...filtered].sort((a, b) => {
      let cmp = 0;
      switch (sort) {
        case 'display_name':
          cmp = a.display_name.localeCompare(b.display_name);
          break;
        case 'status':
          cmp = a.status.localeCompare(b.status);
          break;
        case 'owner_name':
          cmp = a.owner_name.localeCompare(b.owner_name);
          break;
        case 'current_task_title':
          cmp = (a.current_task_title || 'zzz').localeCompare(b.current_task_title || 'zzz');
          break;
        case 'provider':
          cmp = `${a.model} ${a.provider}`.localeCompare(`${b.model} ${b.provider}`);
          break;
        case 'queue_depth':
          cmp = a.telemetry.queue_depth - b.telemetry.queue_depth;
          break;
        case 'success_rate_pct':
          cmp = a.telemetry.success_rate_pct - b.telemetry.success_rate_pct;
          break;
        case 'cost_today_cents':
          cmp = a.telemetry.cost_today_cents - b.telemetry.cost_today_cents;
          break;
        case 'health':
          cmp = store.computeEffectiveHealth(a).localeCompare(store.computeEffectiveHealth(b));
          break;
        default:
          cmp = a.agent_id.localeCompare(b.agent_id);
      }
      if (cmp === 0) cmp = a.agent_id.localeCompare(b.agent_id);
      return sort_dir === 'desc' ? -cmp : cmp;
    });

    const total = sorted.length;
    const total_pages = Math.max(1, Math.ceil(total / page_size));
    const safePage = Math.min(page, total_pages);
    const start = (safePage - 1) * page_size;
    const pageItems = sorted.slice(start, start + page_size).map((a) => ({
      ...a,
      health: store.computeEffectiveHealth(a),
      cost_today_formatted: formatCentsUsd(a.telemetry.cost_today_cents),
    }));

    // Facets derived from full tenant workspace scope
    const allScoped = store.filterAgents(ctx.tenant_id, { workspace });
    const teams = Array.from(new Map(allScoped.map((a) => [a.team_id, { id: a.team_id, name: a.team_name }])).values());
    const owners = Array.from(new Map(allScoped.map((a) => [a.owner_id, { id: a.owner_id, name: a.owner_name }])).values());
    const providers = Array.from(new Set(allScoped.map((a) => a.provider))).sort();

    res.json({
      items: pageItems,
      total,
      page: safePage,
      page_size,
      total_pages,
      group_by,
      facets: { teams, owners, providers },
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/export', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'export:run')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role lacks export permission.', ctx.correlation_id);
    }
    const format = req.query.format === 'json' ? 'json' : 'csv';
    const list = store.filterAgents(ctx.tenant_id, {
      workspace: typeof req.query.workspace === 'string' ? req.query.workspace : undefined,
      query: typeof req.query.query === 'string' ? req.query.query : undefined,
      status: typeof req.query.status === 'string' ? req.query.status : undefined,
      team_id: typeof req.query.team_id === 'string' ? req.query.team_id : undefined,
      owner_id: typeof req.query.owner_id === 'string' ? req.query.owner_id : undefined,
      provider: typeof req.query.provider === 'string' ? req.query.provider : undefined,
      kpi_filter: typeof req.query.kpi_filter === 'string' ? req.query.kpi_filter : undefined,
    });

    store.recordAudit(ctx, {
      action: `agents.export.${format}`,
      entity_type: 'agent',
      entity_id: `batch:${list.length}`,
      reason: `Exported ${list.length} agent records (${format.toUpperCase()})`,
      before_state: null,
      after_state: { count: list.length, format },
      approval_id: null,
      result: 'success',
    });

    if (format === 'json') {
      res.setHeader('Content-Type', 'application/json');
      return res.json({
        data_as_of: new Date().toISOString(),
        tenant_id: ctx.tenant_id,
        count: list.length,
        items: list,
      });
    }

    const headers = ['agent_id', 'display_name', 'role', 'status', 'health', 'owner_name', 'team_name', 'current_task_title', 'provider', 'model', 'queue_depth', 'success_rate_pct', 'cost_today_usd'];
    const rows = [
      headers.join(','),
      ...list.map((a) =>
        [
          a.agent_id,
          a.display_name,
          a.role,
          a.status,
          store.computeEffectiveHealth(a),
          a.owner_name,
          a.team_name,
          a.current_task_title || '',
          a.provider,
          a.model,
          a.telemetry.queue_depth,
          a.telemetry.success_rate_pct,
          formatCentsUsd(a.telemetry.cost_today_cents),
        ]
          .map(escapeCsv)
          .join(','),
      ),
    ];
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="agent-workforce.csv"');
    return res.send(rows.join('\n'));
  });

  router.post('/agents', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:write')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot register agents.', ctx.correlation_id);
    }
    const body = req.body ?? {};
    const display_name = String(body.display_name || '').trim();
    const role = String(body.role || '').trim();
    const provider = String(body.provider || 'Google').trim();
    const model = String(body.model || 'Gemini 2.5 Flash').trim();
    const team_name = String(body.team_name || 'Operations').trim();
    const owner_name = String(body.owner_name || ctx.actor_name).trim();
    const risk_level: RiskLevel = ['low', 'medium', 'high', 'critical'].includes(body.risk_level) ? body.risk_level : 'low';

    if (!display_name || !role) {
      return sendError(res, 400, 'VALIDATION_ERROR', 'display_name and role are required.', ctx.correlation_id);
    }

    const nextNum = store.agents.size + 1;
    const agent_id = `AG-${String(nextNum).padStart(3, '0')}`;
    const now = new Date().toISOString();

    const agent: CanonicalAgent = {
      agent_id,
      legacy_bot_id: `bot-${nextNum}`,
      tenant_id: ctx.tenant_id,
      workspace: String(body.workspace || 'Operations'),
      display_name: redactSecrets(display_name),
      role: redactSecrets(role),
      description: redactSecrets(String(body.description || `${display_name} — ${role} agent registered via Command Center.`)),
      version: 1,
      config_version: '2.7.0',
      owner_id: `usr-${owner_name.toLowerCase().replace(/[^a-z0-9]/g, '') || 'owner'}`,
      owner_name,
      team_id: `team-${team_name.toLowerCase().replace(/[^a-z0-9]/g, '') || 'ops'}`,
      team_name,
      assigned_to: team_name,
      status: 'idle',
      health: 'healthy',
      current_task_id: null,
      current_task_title: null,
      parent_goal: String(body.parent_goal || 'Workforce operational coverage'),
      project: String(body.project || 'Agent WASP Operations'),
      priority: 'normal',
      progress_pct: 0,
      next_action: 'Ready for first task assignment',
      blocker: null,
      task_age_minutes: 0,
      handoff_to_agent_id: null,
      provider,
      model,
      endpoint_region: provider === 'Ollama' ? 'local-host' : 'us-central1',
      reasoning_mode: 'balanced',
      context_used: 0,
      context_limit: 128000,
      fallback_model: 'llama3.2:latest',
      skills: [
        {
          skill_id: `sk-${agent_id}-base`,
          name: `${role} Core`,
          version: '1.0.0',
          enabled: true,
          test_status: 'passing',
          last_verified_at: now,
        },
      ],
      tools: [
        {
          tool_id: `tl-${agent_id}-kb`,
          name: 'Knowledge Base',
          scope: 'read:tenant_kb',
          enabled: true,
          integration_health: 'healthy',
        },
      ],
      connected_integrations: [],
      memory_policy: {
        retention_days: 90,
        provenance_required: true,
        access_scope: 'tenant_team',
        redaction_enabled: true,
        last_consolidation_at: now,
        working_memory_items: 0,
        episodic_records: 0,
        knowledge_sources: 1,
        provenance_status: 'verified',
      },
      resource_budget: {
        max_cpu_pct: 60,
        max_ram_mb: Number(body.max_ram_mb) || 1024,
        max_gpu_vram_mb: 0,
        max_tokens_per_day: Number(body.max_tokens_per_day) || 200000,
        daily_cost_limit_cents: Number(body.daily_cost_limit_cents) || 500,
        max_concurrency: 2,
        schedule_cron: '*/30 * * * *',
        heartbeat_timeout_sec: 300,
      },
      telemetry: {
        cpu_pct: 2,
        ram_mb: 160,
        gpu_vram_mb: null,
        active_workers: 0,
        queue_depth: 0,
        tokens_today: 0,
        cost_today_cents: 0,
        quota_remaining_pct: 100,
        success_rate_pct: 100,
        quality_score_pct: 100,
        tasks_completed: 0,
        tasks_failed: 0,
        avg_latency_ms: 0,
        sla_compliance_pct: 100,
        retries_today: 0,
        incidents_open: 0,
        last_successful_run_at: null,
        last_error: null,
        last_heartbeat: now,
        telemetry_available: true,
      },
      risk_level,
      pending_approvals: 0,
      data_sensitivity: 'internal',
      audit_status: 'compliant',
      policy_violations: 0,
      permissions_summary: String(body.permissions_summary || 'Read-only sources'),
      last_heartbeat: now,
      created_at: now,
      updated_at: now,
    };

    store.agents.set(agent.agent_id, agent);

    const audit = store.recordAudit(ctx, {
      action: 'agent.register',
      entity_type: 'agent',
      entity_id: agent.agent_id,
      reason: `Registered agent ${agent.display_name} (${agent.role})`,
      before_state: null,
      after_state: { agent_id: agent.agent_id, status: agent.status, version: agent.version },
      approval_id: null,
      result: 'success',
    });

    store.recordActivity(
      ctx.tenant_id,
      agent.agent_id,
      ctx.actor_id,
      ctx.actor_name,
      'Agent registered',
      agent.display_name,
      'success',
      `Initial version v${agent.version} deployed on ${agent.provider} (${agent.model}).`,
      ctx.correlation_id,
    );

    store.publish({
      event_type: 'agent.updated',
      tenant_id: ctx.tenant_id,
      entity_id: agent.agent_id,
      entity_version: agent.version,
      correlation_id: ctx.correlation_id,
      payload: { status: agent.status },
    });

    return res.status(201).json({
      agent,
      audit_id: audit.audit_id,
      correlation_id: ctx.correlation_id,
    });
  });

  router.post('/agents/bulk-actions', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:control')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot execute bulk workforce actions.', ctx.correlation_id);
    }

    const body = req.body ?? {};
    const agent_ids: string[] = Array.isArray(body.agent_ids) ? body.agent_ids.map(String) : [];
    const action = String(body.action || '');
    const preview = Boolean(body.preview);
    const reason = String(body.reason || `Bulk action: ${action}`);
    const task_title = typeof body.task_title === 'string' ? body.task_title.trim() : '';

    if (agent_ids.length === 0) {
      return sendError(res, 400, 'VALIDATION_ERROR', 'agent_ids must be a non-empty array.', ctx.correlation_id);
    }

    const affected = agent_ids
      .map((id) => store.agents.get(id))
      .filter((a): a is CanonicalAgent => Boolean(a && a.tenant_id === ctx.tenant_id));

    if (preview) {
      return res.json({
        preview: true,
        action,
        requested_count: agent_ids.length,
        eligible_agents: affected.map((a) => ({
          agent_id: a.agent_id,
          display_name: a.display_name,
          current_status: a.status,
          risk_level: a.risk_level,
        })),
        requires_confirmation: affected.some((a) => a.risk_level === 'high' || a.risk_level === 'critical') || affected.length > 1,
        correlation_id: ctx.correlation_id,
      });
    }

    const results: Array<{ agent_id: string; ok: boolean; status?: string; error?: string; audit_id?: string }> = [];

    for (const agent of affected) {
      if (action === 'assign_task') {
        const title = task_title || 'Operational verification task';
        const task_id = `TSK-${Math.floor(200 + Math.random() * 799)}`;
        const now = new Date().toISOString();
        const task: TaskRecord = {
          task_id,
          tenant_id: ctx.tenant_id,
          workspace: agent.workspace,
          title,
          objective: `Assigned via bulk workforce command: ${reason}`,
          parent_goal: agent.parent_goal || 'Workforce Operations',
          project: agent.project,
          agent_id: agent.agent_id,
          owner_id: ctx.actor_id,
          priority: 'normal',
          status: 'running',
          progress_pct: 10,
          inputs_summary: 'Operator bulk assignment',
          outputs_summary: null,
          checkpoint_ref: null,
          external_side_effects: 'none',
          evidence: [],
          acceptance_checks: [{ check: 'Completion evidence verified', passed: false }],
          depends_on_task_ids: [],
          handoff_to_agent_id: null,
          handoff_label: null,
          blocker_reason: null,
          attempts: 1,
          correlation_id: ctx.correlation_id,
          created_at: now,
          updated_at: now,
        };
        store.tasks.set(task_id, task);
        agent.current_task_id = task_id;
        agent.current_task_title = title;
        agent.telemetry.queue_depth += 1;
        if (agent.status === 'idle') agent.status = 'running';
        agent.version += 1;
        agent.updated_at = now;
        const audit = store.recordAudit(ctx, {
          action: 'agent.bulk.assign_task',
          entity_type: 'agent',
          entity_id: agent.agent_id,
          reason,
          before_state: null,
          after_state: { task_id, title, status: agent.status },
          approval_id: null,
          result: 'success',
        });
        store.recordActivity(ctx.tenant_id, agent.agent_id, ctx.actor_id, ctx.actor_name, 'Task assigned', `${task_id} ${title}`, 'success', reason, ctx.correlation_id, task_id);
        store.publish({ event_type: 'agent.updated', tenant_id: ctx.tenant_id, entity_id: agent.agent_id, entity_version: agent.version, correlation_id: ctx.correlation_id });
        results.push({ agent_id: agent.agent_id, ok: true, status: agent.status, audit_id: audit.audit_id });
      } else if (action === 'pause' || action === 'resume' || action === 'request_approval' || action === 'safe_stop') {
        const exec = store.executeCommand(ctx, agent.agent_id, {
          command: action as AgentCommandType,
          idempotency_key: `bulk_${ctx.correlation_id}_${agent.agent_id}_${action}`,
          reason,
        });
        if (exec.ok) {
          results.push({ agent_id: agent.agent_id, ok: true, status: exec.result.current_status, audit_id: exec.result.audit_id });
        } else {
          results.push({ agent_id: agent.agent_id, ok: false, error: exec.message });
        }
      } else {
        return sendError(res, 400, 'INVALID_BULK_ACTION', `Unsupported bulk action: ${action}`, ctx.correlation_id);
      }
    }

    return res.json({
      preview: false,
      action,
      results,
      correlation_id: ctx.correlation_id,
    });
  });

  // ── 2. Agent Detail Workspace (All 10 Tabs) ───────────────────────────────

  function getScopedAgent(req: Request, res: Response): { agent: CanonicalAgent; ctx: ReturnType<typeof extractContext> } | null {
    const ctx = extractContext(req);
    const agent = store.agents.get(String(req.params.agent_id));
    if (!agent || agent.tenant_id !== ctx.tenant_id) {
      sendError(res, 404, 'AGENT_NOT_FOUND', `Agent ${String(req.params.agent_id)} not found.`, ctx.correlation_id);
      return null;
    }
    return { agent, ctx };
  }

  router.get('/agents/:agent_id', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    res.json({
      ...agent,
      health: store.computeEffectiveHealth(agent),
      cost_today_formatted: formatCentsUsd(agent.telemetry.cost_today_cents),
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/overview', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const currentTask = agent.current_task_id ? store.tasks.get(agent.current_task_id) ?? null : null;
    const handoffTarget = agent.handoff_to_agent_id ? store.agents.get(agent.handoff_to_agent_id) ?? null : null;
    const recentActivity = store.activities
      .filter((a) => a.tenant_id === ctx.tenant_id && a.agent_id === agent.agent_id)
      .slice(0, 6);

    res.json({
      agent_id: agent.agent_id,
      display_name: agent.display_name,
      role: agent.role,
      description: agent.description,
      version: agent.version,
      config_version: agent.config_version,
      status: agent.status,
      health: store.computeEffectiveHealth(agent),
      owner_name: agent.owner_name,
      team_name: agent.team_name,
      assignment: {
        current_task_id: agent.current_task_id,
        current_task_title: agent.current_task_title,
        parent_goal: agent.parent_goal,
        project: agent.project,
        progress_pct: agent.progress_pct,
        next_action: agent.next_action,
        blocker: agent.blocker,
        handoff_to: handoffTarget
          ? { agent_id: handoffTarget.agent_id, display_name: handoffTarget.display_name, role: handoffTarget.role }
          : null,
        sla_compliance_pct: agent.telemetry.sla_compliance_pct,
        evidence_count: currentTask?.evidence.length ?? 0,
      },
      capabilities_runtime: {
        tools: agent.tools.map((t) => t.name),
        provider: agent.provider,
        model: agent.model,
        ram_mb: agent.telemetry.ram_mb,
        gpu_vram_mb: agent.telemetry.gpu_vram_mb,
        cpu_pct: agent.telemetry.cpu_pct,
        telemetry_available: agent.telemetry.telemetry_available,
        permissions_summary: agent.permissions_summary,
      },
      recent_activity: recentActivity,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/tasks', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const items = Array.from(store.tasks.values()).filter(
      (t) => t.tenant_id === ctx.tenant_id && t.agent_id === agent.agent_id,
    );
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.get('/agents/:agent_id/skills-tools', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const integrations = agent.connected_integrations
      .map((id) => Array.from(store.integrations.values()).find((i) => i.integration_id === id || i.integration_id === `int-${id}` || i.name.toLowerCase().includes(id.replace('-', ' '))))
      .filter(Boolean);
    res.json({
      agent_id: agent.agent_id,
      skills: agent.skills,
      tools: agent.tools,
      permissions_summary: agent.permissions_summary,
      integrations,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/model-usage', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    res.json({
      agent_id: agent.agent_id,
      provider: agent.provider,
      model: agent.model,
      endpoint_region: agent.endpoint_region,
      reasoning_mode: agent.reasoning_mode,
      context_used: agent.context_used,
      context_limit: agent.context_limit,
      context_utilization_pct: Math.round((agent.context_used / Math.max(1, agent.context_limit)) * 100),
      fallback_model: agent.fallback_model,
      tokens_today: agent.telemetry.tokens_today,
      max_tokens_per_day: agent.resource_budget.max_tokens_per_day,
      cost_today_cents: agent.telemetry.cost_today_cents,
      cost_today_formatted: formatCentsUsd(agent.telemetry.cost_today_cents),
      daily_cost_limit_cents: agent.resource_budget.daily_cost_limit_cents,
      daily_cost_limit_formatted: formatCentsUsd(agent.resource_budget.daily_cost_limit_cents),
      quota_remaining_pct: agent.telemetry.quota_remaining_pct,
      avg_latency_ms: agent.telemetry.avg_latency_ms,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/memory', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const canReadRestricted = hasPermission(ctx.actor_role, 'memory:read_restricted');
    const allForAgent = store.memories.filter((m) => m.tenant_id === ctx.tenant_id && m.agent_id === agent.agent_id);
    const visible = allForAgent.filter((m) => m.sensitivity !== 'restricted' || canReadRestricted);
    const omitted_restricted_count = allForAgent.length - visible.length;

    res.json({
      agent_id: agent.agent_id,
      policy: agent.memory_policy,
      items: visible,
      omitted_restricted_count,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/activity', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const items = store.activities.filter((a) => a.tenant_id === ctx.tenant_id && a.agent_id === agent.agent_id);
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.get('/agents/:agent_id/quality', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const evaluations = store.evaluations.filter((e) => e.agent_id === agent.agent_id);
    res.json({
      agent_id: agent.agent_id,
      success_rate_pct: agent.telemetry.success_rate_pct,
      quality_score_pct: agent.telemetry.quality_score_pct,
      tasks_completed: agent.telemetry.tasks_completed,
      tasks_failed: agent.telemetry.tasks_failed,
      sla_compliance_pct: agent.telemetry.sla_compliance_pct,
      evaluations,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/agents/:agent_id/incidents', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const items = Array.from(store.incidents.values()).filter(
      (i) => i.tenant_id === ctx.tenant_id && i.agent_id === agent.agent_id,
    );
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.get('/agents/:agent_id/governance', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    const approvals = Array.from(store.approvals.values()).filter(
      (a) => a.tenant_id === ctx.tenant_id && a.agent_id === agent.agent_id,
    );
    const audits = store.audits
      .filter((a) => a.tenant_id === ctx.tenant_id && a.entity_id === agent.agent_id)
      .slice(0, 20);

    res.json({
      agent_id: agent.agent_id,
      owner_name: agent.owner_name,
      risk_level: agent.risk_level,
      data_sensitivity: agent.data_sensitivity,
      audit_status: agent.audit_status,
      policy_violations: agent.policy_violations,
      permissions_summary: agent.permissions_summary,
      approvals,
      audit_trail: audits,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.patch('/agents/:agent_id/configuration', (req: Request, res: Response) => {
    const found = getScopedAgent(req, res);
    if (!found) return;
    const { agent, ctx } = found;
    if (!hasPermission(ctx.actor_role, 'agent:write')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot modify agent configuration.', ctx.correlation_id);
    }

    const body = req.body ?? {};
    const expected_version = Number(body.expected_version);
    if (!Number.isFinite(expected_version)) {
      return sendError(res, 400, 'VALIDATION_ERROR', 'expected_version is required for optimistic locking.', ctx.correlation_id);
    }
    if (expected_version !== agent.version) {
      store.recordAudit(ctx, {
        action: 'agent.configuration.conflict',
        entity_type: 'configuration',
        entity_id: agent.agent_id,
        reason: `Version conflict: expected ${expected_version}, actual ${agent.version}`,
        before_state: { version: agent.version },
        after_state: null,
        approval_id: null,
        result: 'conflict',
      });
      return sendError(
        res,
        409,
        'VERSION_CONFLICT',
        `Configuration was modified by another supervisor. Expected version ${expected_version}, current version is ${agent.version}.`,
        ctx.correlation_id,
        true,
        { current_version: agent.version },
      );
    }

    const before = {
      model: agent.model,
      fallback_model: agent.fallback_model,
      reasoning_mode: agent.reasoning_mode,
      resource_budget: { ...agent.resource_budget },
      version: agent.version,
    };

    if (typeof body.model === 'string' && body.model.trim()) agent.model = body.model.trim();
    if (typeof body.fallback_model === 'string' && body.fallback_model.trim()) agent.fallback_model = body.fallback_model.trim();
    if (['fast', 'balanced', 'deep'].includes(body.reasoning_mode)) agent.reasoning_mode = body.reasoning_mode;
    if (typeof body.max_ram_mb === 'number' && body.max_ram_mb >= 128 && body.max_ram_mb <= 16384) {
      agent.resource_budget.max_ram_mb = body.max_ram_mb;
    }
    if (typeof body.daily_cost_limit_cents === 'number' && body.daily_cost_limit_cents >= 0) {
      agent.resource_budget.daily_cost_limit_cents = Math.round(body.daily_cost_limit_cents);
    }
    if (typeof body.max_concurrency === 'number' && body.max_concurrency >= 1 && body.max_concurrency <= 16) {
      agent.resource_budget.max_concurrency = Math.round(body.max_concurrency);
    }
    if (typeof body.schedule_cron === 'string' && body.schedule_cron.trim()) {
      agent.resource_budget.schedule_cron = body.schedule_cron.trim();
    }

    agent.version += 1;
    agent.updated_at = new Date().toISOString();

    const audit = store.recordAudit(ctx, {
      action: 'agent.configuration.updated',
      entity_type: 'configuration',
      entity_id: agent.agent_id,
      reason: String(body.reason || 'Supervisor configuration update'),
      before_state: before,
      after_state: {
        model: agent.model,
        fallback_model: agent.fallback_model,
        reasoning_mode: agent.reasoning_mode,
        resource_budget: agent.resource_budget,
        version: agent.version,
      },
      approval_id: null,
      result: 'success',
    });

    store.recordActivity(
      ctx.tenant_id,
      agent.agent_id,
      ctx.actor_id,
      ctx.actor_name,
      'Configuration updated',
      `${agent.display_name} v${agent.version}`,
      'success',
      String(body.reason || 'Updated runtime limits and model configuration.'),
      ctx.correlation_id,
    );

    store.publish({
      event_type: 'agent.updated',
      tenant_id: ctx.tenant_id,
      entity_id: agent.agent_id,
      entity_version: agent.version,
      correlation_id: ctx.correlation_id,
    });

    return res.json({
      agent,
      audit_id: audit.audit_id,
      correlation_id: ctx.correlation_id,
    });
  });

  router.post('/agents/:agent_id/commands', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:control')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot issue agent control commands.', ctx.correlation_id);
    }

    const body = req.body ?? {};
    const command = String(body.command || '') as AgentCommandType;
    const idempotency_key = String(body.idempotency_key || req.header('idempotency-key') || '').trim();
    const reason = String(body.reason || `Operator issued ${command}`).trim();

    const validCommands: AgentCommandType[] = ['pause', 'resume', 'cancel', 'safe_stop', 'retry', 'disable', 'rollback', 'request_approval'];
    if (!validCommands.includes(command)) {
      return sendError(res, 400, 'INVALID_COMMAND', `Unsupported command: ${command}`, ctx.correlation_id);
    }
    if (!idempotency_key) {
      return sendError(res, 400, 'IDEMPOTENCY_KEY_REQUIRED', 'A unique idempotency_key is required for agent commands.', ctx.correlation_id);
    }

    const exec = store.executeCommand(ctx, String(req.params.agent_id), {
      command,
      idempotency_key,
      expected_version: typeof body.expected_version === 'number' ? body.expected_version : undefined,
      reason,
      approval_id: typeof body.approval_id === 'string' ? body.approval_id : undefined,
    });

    if (!exec.ok) {
      return sendError(res, exec.status, exec.code, exec.message, ctx.correlation_id, exec.status === 409);
    }

    return res.json(exec.result);
  });

  // ── 3. Tasks & Approval Gates ─────────────────────────────────────────────

  router.get('/tasks', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const status = typeof req.query.status === 'string' ? req.query.status : 'all';
    const items = Array.from(store.tasks.values()).filter(
      (t) => t.tenant_id === ctx.tenant_id && (status === 'all' || t.status === status),
    );
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.patch('/tasks/:task_id', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:write')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot modify tasks.', ctx.correlation_id);
    }
    const task = store.tasks.get(String(req.params.task_id));
    if (!task || task.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'TASK_NOT_FOUND', 'Task not found.', ctx.correlation_id);
    }

    const body = req.body ?? {};
    const nextStatus = body.status as TaskStatus | undefined;

    if (nextStatus && nextStatus !== task.status) {
      if (!isValidTaskTransition(task.status, nextStatus)) {
        return sendError(
          res,
          409,
          'INVALID_TASK_TRANSITION',
          `Cannot transition task from '${task.status}' to '${nextStatus}'.`,
          ctx.correlation_id,
        );
      }

      if (nextStatus === 'completed') {
        const evidence = Array.isArray(body.evidence) ? body.evidence : task.evidence;
        const checks = Array.isArray(body.acceptance_checks) ? body.acceptance_checks : task.acceptance_checks;
        const allPassed = checks.length > 0 && checks.every((c: { passed?: boolean }) => Boolean(c.passed));
        if (!evidence || evidence.length === 0 || !allPassed) {
          return sendError(
            res,
            422,
            'COMPLETION_EVIDENCE_REQUIRED',
            'A task cannot become COMPLETED without non-empty completion evidence and all acceptance checks passing.',
            ctx.correlation_id,
          );
        }
        task.evidence = evidence;
        task.acceptance_checks = checks;
        task.progress_pct = 100;
      }

      const prev = task.status;
      task.status = nextStatus;
      task.updated_at = new Date().toISOString();

      store.recordAudit(ctx, {
        action: 'task.status.updated',
        entity_type: 'task',
        entity_id: task.task_id,
        reason: String(body.reason || `Transitioned task ${prev} → ${nextStatus}`),
        before_state: { status: prev },
        after_state: { status: nextStatus },
        approval_id: null,
        result: 'success',
      });
    }

    if (typeof body.agent_id === 'string' && body.agent_id !== task.agent_id) {
      const targetAgent = store.agents.get(body.agent_id);
      if (!targetAgent || targetAgent.tenant_id !== ctx.tenant_id) {
        return sendError(res, 404, 'AGENT_NOT_FOUND', 'Assigned agent not found.', ctx.correlation_id);
      }
      task.agent_id = targetAgent.agent_id;
      task.blocker_reason = null;
      if (task.status === 'queued') task.status = 'assigned';
      task.updated_at = new Date().toISOString();
      targetAgent.current_task_id = task.task_id;
      targetAgent.current_task_title = task.title;
      targetAgent.version += 1;
      store.publish({
        event_type: 'task.updated',
        tenant_id: ctx.tenant_id,
        entity_id: task.task_id,
        entity_version: targetAgent.version,
        correlation_id: ctx.correlation_id,
      });
    }

    return res.json({ task, correlation_id: ctx.correlation_id });
  });

  router.post('/approvals/:approval_id/decide', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'approval:decide')) {
      return sendError(res, 403, 'FORBIDDEN', 'Only supervisors may decide governed approvals.', ctx.correlation_id);
    }
    const apr = store.approvals.get(String(req.params.approval_id));
    if (!apr || apr.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'APPROVAL_NOT_FOUND', 'Approval record not found.', ctx.correlation_id);
    }
    const decision = req.body?.decision === 'approved' ? 'approved' : req.body?.decision === 'rejected' ? 'rejected' : null;
    if (!decision) {
      return sendError(res, 400, 'VALIDATION_ERROR', 'decision must be "approved" or "rejected".', ctx.correlation_id);
    }
    const note = String(req.body?.note || `${decision} by ${ctx.actor_name}`);

    apr.status = decision;
    apr.decided_by = ctx.actor_name;
    apr.decision_note = note;
    apr.decided_at = new Date().toISOString();

    const agent = store.agents.get(apr.agent_id);
    if (agent && agent.tenant_id === ctx.tenant_id) {
      agent.pending_approvals = Math.max(0, agent.pending_approvals - 1);
      if (agent.status === 'awaiting_approval') {
        agent.status = decision === 'approved' ? 'running' : 'paused';
      }
      agent.version += 1;
      agent.updated_at = new Date().toISOString();
      store.publish({
        event_type: 'approval.updated',
        tenant_id: ctx.tenant_id,
        entity_id: agent.agent_id,
        entity_version: agent.version,
        correlation_id: ctx.correlation_id,
        payload: { approval_id: apr.approval_id, decision },
      });
    }

    const audit = store.recordAudit(ctx, {
      action: `approval.${decision}`,
      entity_type: 'approval',
      entity_id: apr.approval_id,
      reason: note,
      before_state: { status: 'pending' },
      after_state: { status: decision, decided_by: ctx.actor_name },
      approval_id: apr.approval_id,
      result: 'success',
    });

    return res.json({ approval: apr, audit_id: audit.audit_id, correlation_id: ctx.correlation_id });
  });

  // ── 4. Organization, Handoffs & Graphify ──────────────────────────────────

  router.get('/organization/graph', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const team = typeof req.query.team === 'string' ? req.query.team : 'all';
    const agents = store.filterAgents(ctx.tenant_id, { team_id: team === 'all' ? undefined : team });

    const supervisorsMap = new Map<string, { id: string; label: string; subtitle: string; type: 'supervisor' }>();
    for (const a of agents) {
      if (!supervisorsMap.has(a.owner_id)) {
        supervisorsMap.set(a.owner_id, {
          id: a.owner_id,
          label: a.owner_name,
          subtitle: 'Human supervisor',
          type: 'supervisor',
        });
      }
    }

    const nodes = [
      ...Array.from(supervisorsMap.values()),
      ...agents.map((a) => ({
        id: a.agent_id,
        label: a.display_name,
        subtitle: a.role,
        type: 'agent' as const,
        status: a.status,
        health: store.computeEffectiveHealth(a),
        owner_id: a.owner_id,
        team_name: a.team_name,
      })),
      {
        id: 'SK-01',
        label: 'Shared knowledge',
        subtitle: `Graphify v${store.graphify.graph_version}`,
        type: 'knowledge' as const,
      },
    ];

    const edges: Array<{
      id: string;
      source: string;
      target: string;
      kind: 'reports_to' | 'handoff' | 'knowledge' | 'blocked';
      label?: string;
      source_record_id: string;
    }> = [];

    for (const a of agents) {
      edges.push({
        id: `rep-${a.owner_id}-${a.agent_id}`,
        source: a.owner_id,
        target: a.agent_id,
        kind: 'reports_to',
        source_record_id: a.agent_id,
      });
      if (a.handoff_to_agent_id && agents.some((x) => x.agent_id === a.handoff_to_agent_id)) {
        const task = a.current_task_id ? store.tasks.get(a.current_task_id) : null;
        edges.push({
          id: `ho-${a.agent_id}-${a.handoff_to_agent_id}`,
          source: a.agent_id,
          target: a.handoff_to_agent_id,
          kind: 'handoff',
          label: task?.handoff_label || 'Task handoff',
          source_record_id: task?.task_id || a.agent_id,
        });
      }
      if (a.agent_id === 'AG-001' || a.agent_id === 'AG-004' || a.agent_id === 'AG-002') {
        edges.push({
          id: `kn-${a.agent_id}-SK01`,
          source: a.agent_id,
          target: 'SK-01',
          kind: 'knowledge',
          source_record_id: a.agent_id,
        });
      }
    }

    const blocked_dependencies = agents
      .filter((a) => a.status === 'blocked' || a.blocker)
      .map((a) => ({
        agent_id: a.agent_id,
        display_name: a.display_name,
        target: a.agent_id === 'AG-003' ? 'Sheets' : a.connected_integrations[0] || 'Dependency',
        summary: a.blocker || 'Blocked dependency',
        incident_id: Array.from(store.incidents.values()).find((i) => i.agent_id === a.agent_id && i.status !== 'resolved')?.incident_id ?? null,
      }));

    res.json({
      team,
      nodes,
      edges,
      blocked_dependencies,
      updated_at: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/tasks/handoffs', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const tasks = Array.from(store.tasks.values()).filter((t) => t.tenant_id === ctx.tenant_id);
    const agents = store.filterAgents(ctx.tenant_id, {});

    const handoffs = tasks
      .filter((t) => t.agent_id && t.handoff_to_agent_id)
      .map((t) => ({
        task_id: t.task_id,
        title: t.title,
        from_agent_id: t.agent_id!,
        from_agent_name: store.agents.get(t.agent_id!)?.display_name ?? t.agent_id!,
        to_agent_id: t.handoff_to_agent_id!,
        to_agent_name: store.agents.get(t.handoff_to_agent_id!)?.display_name ?? t.handoff_to_agent_id!,
        label: t.handoff_label || 'Task handoff',
        status: t.status,
      }));

    const unowned_tasks = tasks.filter((t) => !t.agent_id && t.status !== 'completed' && t.status !== 'cancelled');
    const overloaded_agents = agents.filter((a) => a.telemetry.queue_depth >= 4);
    const blocked_tasks = tasks.filter((t) => t.status === 'blocked');

    // Simple cycle detection across task handoff graph
    const adj = new Map<string, string[]>();
    for (const h of handoffs) {
      adj.set(h.from_agent_id, [...(adj.get(h.from_agent_id) ?? []), h.to_agent_id]);
    }
    const circular_handoffs: string[][] = [];
    const visited = new Set<string>();
    const stack = new Set<string>();
    const dfs = (node: string, path: string[]) => {
      visited.add(node);
      stack.add(node);
      for (const nxt of adj.get(node) ?? []) {
        if (stack.has(nxt)) {
          circular_handoffs.push([...path.slice(path.indexOf(nxt)), nxt]);
        } else if (!visited.has(nxt)) {
          dfs(nxt, [...path, nxt]);
        }
      }
      stack.delete(node);
    };
    for (const node of adj.keys()) {
      if (!visited.has(node)) dfs(node, [node]);
    }

    res.json({
      handoffs,
      unowned_tasks,
      overloaded_agents: overloaded_agents.map((a) => ({ agent_id: a.agent_id, display_name: a.display_name, queue_depth: a.telemetry.queue_depth })),
      blocked_tasks,
      circular_handoffs,
      data_as_of: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/graph/nodes/:node_id', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const nodeId = String(req.params.node_id);

    const agent = store.agents.get(nodeId);
    if (agent) {
      if (agent.tenant_id !== ctx.tenant_id) {
        return sendError(res, 404, 'NODE_NOT_FOUND', 'Node not found in tenant scope.', ctx.correlation_id);
      }
      return res.json({
        node_type: 'agent',
        node_id: agent.agent_id,
        record: { ...agent, health: store.computeEffectiveHealth(agent) },
        correlation_id: ctx.correlation_id,
      });
    }

    const task = store.tasks.get(nodeId);
    if (task && task.tenant_id === ctx.tenant_id) {
      return res.json({ node_type: 'task', node_id: task.task_id, record: task, correlation_id: ctx.correlation_id });
    }

    if (nodeId === 'SK-01') {
      const canReadRestricted = hasPermission(ctx.actor_role, 'memory:read_restricted');
      const items = store.memories.filter((m) => m.tenant_id === ctx.tenant_id && (m.sensitivity !== 'restricted' || canReadRestricted));
      return res.json({
        node_type: 'knowledge',
        node_id: 'SK-01',
        record: { graphify: store.graphify, memory_items_count: items.length, recent_memories: items.slice(0, 5) },
        correlation_id: ctx.correlation_id,
      });
    }

    const supervised = store.filterAgents(ctx.tenant_id, { owner_id: nodeId });
    if (supervised.length > 0) {
      return res.json({
        node_type: 'supervisor',
        node_id: nodeId,
        record: {
          owner_id: nodeId,
          owner_name: supervised[0].owner_name,
          direct_reports: supervised.map((a) => ({ agent_id: a.agent_id, display_name: a.display_name, role: a.role, status: a.status })),
        },
        correlation_id: ctx.correlation_id,
      });
    }

    return sendError(res, 404, 'NODE_NOT_FOUND', `Graph node ${nodeId} not found.`, ctx.correlation_id);
  });

  router.get('/graph/graphify', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const jobs = Array.from(store.graphJobs.values())
      .filter((j) => j.tenant_id === ctx.tenant_id)
      .slice(-5)
      .reverse();
    res.json({ snapshot: store.graphify, recent_jobs: jobs, correlation_id: ctx.correlation_id });
  });

  router.post('/graph/refresh', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:write')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot trigger Graphify refresh.', ctx.correlation_id);
    }
    const simulate_failure = Boolean(req.body?.simulate_failure);
    const job = store.startGraphRefresh(ctx, { simulate_failure });
    res.status(202).json({ job, correlation_id: ctx.correlation_id });
  });

  router.get('/graph/refresh/:job_id', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const job = store.graphJobs.get(String(req.params.job_id));
    if (!job || job.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'JOB_NOT_FOUND', 'Graph refresh job not found.', ctx.correlation_id);
    }
    res.json({ job, current_snapshot: store.graphify, correlation_id: ctx.correlation_id });
  });

  router.post('/graph/refresh/:job_id/cancel', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const job = store.graphJobs.get(String(req.params.job_id));
    if (!job || job.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'JOB_NOT_FOUND', 'Graph refresh job not found.', ctx.correlation_id);
    }
    if (job.status === 'running' || job.status === 'queued' || job.status === 'validating') {
      job.status = 'cancelled';
      job.completed_at = new Date().toISOString();
    }
    res.json({ job, correlation_id: ctx.correlation_id });
  });

  // ── 5. Integrations, Incidents, Audit, Resources & Live SSE ───────────────

  router.get('/integrations', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const items = Array.from(store.integrations.values()).filter((i) => i.tenant_id === ctx.tenant_id);
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.post('/integrations/:integration_id/sync', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'connector:manage')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot trigger connector syncs.', ctx.correlation_id);
    }
    const connector = store.integrations.get(String(req.params.integration_id));
    if (!connector || connector.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'INTEGRATION_NOT_FOUND', 'Integration not found.', ctx.correlation_id);
    }
    const dedup_key = String(req.body?.idempotency_key || `sync_${connector.integration_id}_${Date.now()}`);
    const scopedKey = `${ctx.tenant_id}:${connector.integration_id}:${dedup_key}`;
    if (store.integrationDedupKeys.has(scopedKey)) {
      return res.json({
        ok: true,
        deduplicated: true,
        integration: connector,
        correlation_id: ctx.correlation_id,
      });
    }
    store.integrationDedupKeys.add(scopedKey);
    connector.sync_runs += 1;
    connector.status = 'healthy';
    connector.last_error = null;
    connector.last_success_at = new Date().toISOString();
    connector.dead_letter_count = 0;
    connector.rate_limit_remaining_pct = Math.max(75, connector.rate_limit_remaining_pct);

    // Clear blocked dependency on connected agents if Sheets/Calendar recovered
    for (const agentId of connector.connected_agents) {
      const a = store.agents.get(agentId);
      if (a && a.tenant_id === ctx.tenant_id && (a.health === 'api_issue' || a.health === 'degraded')) {
        a.health = 'healthy';
        a.telemetry.last_error = null;
        if (a.status === 'blocked') {
          a.status = 'running';
          a.blocker = null;
        }
        a.version += 1;
        a.updated_at = new Date().toISOString();
        store.publish({
          event_type: 'agent.updated',
          tenant_id: ctx.tenant_id,
          entity_id: a.agent_id,
          entity_version: a.version,
          correlation_id: ctx.correlation_id,
        });
      }
    }

    const audit = store.recordAudit(ctx, {
      action: 'integration.sync.reconciled',
      entity_type: 'integration',
      entity_id: connector.integration_id,
      reason: `Idempotent connector sync & dead-letter replay (${dedup_key})`,
      before_state: null,
      after_state: { status: connector.status, sync_runs: connector.sync_runs },
      approval_id: null,
      result: 'success',
    });

    return res.json({
      ok: true,
      deduplicated: false,
      integration: connector,
      audit_id: audit.audit_id,
      correlation_id: ctx.correlation_id,
    });
  });

  router.get('/incidents', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    const items = Array.from(store.incidents.values()).filter((i) => i.tenant_id === ctx.tenant_id);
    res.json({ items, total: items.length, data_as_of: new Date().toISOString(), correlation_id: ctx.correlation_id });
  });

  router.post('/incidents/:incident_id/resolve', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'agent:control')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot resolve incidents.', ctx.correlation_id);
    }
    const inc = store.incidents.get(String(req.params.incident_id));
    if (!inc || inc.tenant_id !== ctx.tenant_id) {
      return sendError(res, 404, 'INCIDENT_NOT_FOUND', 'Incident not found.', ctx.correlation_id);
    }
    inc.status = 'resolved';
    inc.fix_summary = String(req.body?.fix_summary || 'Verified remediation and cleared incident.');
    inc.resolved_at = new Date().toISOString();

    const agent = store.agents.get(inc.agent_id);
    if (agent && agent.tenant_id === ctx.tenant_id) {
      agent.telemetry.incidents_open = Math.max(0, agent.telemetry.incidents_open - 1);
      agent.version += 1;
      store.publish({
        event_type: 'agent.updated',
        tenant_id: ctx.tenant_id,
        entity_id: agent.agent_id,
        entity_version: agent.version,
        correlation_id: ctx.correlation_id,
      });
    }

    const audit = store.recordAudit(ctx, {
      action: 'incident.resolved',
      entity_type: 'incident',
      entity_id: inc.incident_id,
      reason: inc.fix_summary,
      before_state: { status: 'open' },
      after_state: { status: 'resolved' },
      approval_id: null,
      result: 'success',
    });

    res.json({ incident: inc, audit_id: audit.audit_id, correlation_id: ctx.correlation_id });
  });

  router.get('/audit', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    if (!hasPermission(ctx.actor_role, 'audit:read')) {
      return sendError(res, 403, 'FORBIDDEN', 'Role cannot read audit trail.', ctx.correlation_id);
    }
    const q = typeof req.query.query === 'string' ? req.query.query.trim().toLowerCase() : '';
    const items = store.audits.filter(
      (a) =>
        a.tenant_id === ctx.tenant_id &&
        (!q ||
          a.action.toLowerCase().includes(q) ||
          a.entity_id.toLowerCase().includes(q) ||
          a.actor_name.toLowerCase().includes(q) ||
          a.correlation_id.toLowerCase().includes(q) ||
          a.reason.toLowerCase().includes(q)),
    );
    res.json({ items: items.slice(0, 100), total: items.length, correlation_id: ctx.correlation_id });
  });

  router.get('/resources', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    res.json({ ...store.getResourceDashboard(ctx.tenant_id), correlation_id: ctx.correlation_id });
  });

  router.get('/events/stream', (req: Request, res: Response) => {
    const ctx = extractContext(req);
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders?.();

    const hello = {
      event_id: `evt_${crypto.randomBytes(4).toString('hex')}`,
      event_type: 'agent.heartbeat',
      tenant_id: ctx.tenant_id,
      entity_id: 'workforce',
      entity_version: 1,
      occurred_at: new Date().toISOString(),
      correlation_id: ctx.correlation_id,
    };
    res.write(`id: ${hello.event_id}\ndata: ${JSON.stringify(hello)}\n\n`);

    const unsubscribe = store.subscribe(ctx.tenant_id, (evt) => {
      res.write(`id: ${evt.event_id}\ndata: ${JSON.stringify(evt)}\n\n`);
    });

    const keepAlive = setInterval(() => {
      res.write(`: keep-alive ${Date.now()}\n\n`);
    }, 20000);

    req.on('close', () => {
      clearInterval(keepAlive);
      unsubscribe();
    });
  });

  return router;
}
