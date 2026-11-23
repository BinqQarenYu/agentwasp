import React, { useEffect, useState } from 'react';
import {
  ArrowRight,
  ChevronDown,
  Pause,
  Play,
  RotateCcw,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sliders,
} from 'lucide-react';
import type {
  ActivityEvent,
  AgentCommandType,
  ApprovalRecord,
  AuditEvent,
  CanonicalAgent,
  IncidentRecord,
  MemoryItem,
  QualityEvaluation,
  TaskRecord,
} from '../shared/types';
import { apiRequest, makeIdempotencyKey } from '../api/client';
import { AgentHealthChip, AgentStatusChip, formatShortTime, RiskText } from './StatusBadge';

type DetailTab =
  | 'overview'
  | 'tasks'
  | 'skills-tools'
  | 'model-usage'
  | 'memory'
  | 'activity'
  | 'quality'
  | 'incidents'
  | 'governance'
  | 'configuration';

const TABS: Array<{ id: DetailTab; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'skills-tools', label: 'Skills & Tools' },
  { id: 'model-usage', label: 'Model' },
  { id: 'memory', label: 'Memory' },
  { id: 'activity', label: 'Activity' },
  { id: 'quality', label: 'Quality' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'governance', label: 'Governance' },
  { id: 'configuration', label: 'Configuration' },
];

interface AgentDetailWorkspaceProps {
  agentId: string | null;
  refreshNonce: number;
  onSelectAgent: (id: string) => void;
  onAgentMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}

export function AgentDetailWorkspace({
  agentId,
  refreshNonce,
  onSelectAgent,
  onAgentMutated,
  onNotify,
}: AgentDetailWorkspaceProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>('overview');
  const [agent, setAgent] = useState<CanonicalAgent | null>(null);
  const [tabData, setTabData] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [cmdBusy, setCmdBusy] = useState(false);

  // Configuration form state
  const [cfgModel, setCfgModel] = useState('');
  const [cfgFallback, setCfgFallback] = useState('');
  const [cfgReasoning, setCfgReasoning] = useState<'fast' | 'balanced' | 'deep'>('balanced');
  const [cfgRam, setCfgRam] = useState(1024);
  const [cfgCostCents, setCfgCostCents] = useState(1000);
  const [cfgConcurrency, setCfgConcurrency] = useState(3);
  const [cfgCron, setCfgCron] = useState('');
  const [cfgSaving, setCfgSaving] = useState(false);

  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      apiRequest<CanonicalAgent>(`/agents/${agentId}`),
      activeTab === 'configuration'
        ? Promise.resolve(null)
        : apiRequest<Record<string, unknown>>(`/agents/${agentId}/${activeTab}`),
    ])
      .then(([canonical, tabPayload]) => {
        if (cancelled) return;
        setAgent(canonical);
        setTabData(tabPayload);
        setCfgModel(canonical.model);
        setCfgFallback(canonical.fallback_model);
        setCfgReasoning(canonical.reasoning_mode);
        setCfgRam(canonical.resource_budget.max_ram_mb);
        setCfgCostCents(canonical.resource_budget.daily_cost_limit_cents);
        setCfgConcurrency(canonical.resource_budget.max_concurrency);
        setCfgCron(canonical.resource_budget.schedule_cron);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load agent workspace');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [agentId, activeTab, refreshNonce]);

  const sendCommand = async (command: AgentCommandType, reason: string) => {
    if (!agent) return;
    setMoreOpen(false);
    setCmdBusy(true);
    try {
      const res = await apiRequest<{
        command_id: string;
        current_status: string;
        checkpoint_ref: string | null;
        audit_id: string;
      }>(`/agents/${agent.agent_id}/commands`, {
        method: 'POST',
        body: {
          command,
          idempotency_key: makeIdempotencyKey(`ws_${command}`),
          expected_version: agent.version,
          reason,
        },
      });
      onNotify(
        `${agent.display_name}: ${command} → ${res.current_status}${res.checkpoint_ref ? ` (${res.checkpoint_ref})` : ''} · Audit ${res.audit_id}`,
        'success',
      );
      onAgentMutated();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Command rejected', 'error');
    } finally {
      setCmdBusy(false);
    }
  };

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agent) return;
    setCfgSaving(true);
    try {
      const res = await apiRequest<{ agent: CanonicalAgent; audit_id: string }>(
        `/agents/${agent.agent_id}/configuration`,
        {
          method: 'PATCH',
          body: {
            expected_version: agent.version,
            model: cfgModel,
            fallback_model: cfgFallback,
            reasoning_mode: cfgReasoning,
            max_ram_mb: Number(cfgRam),
            daily_cost_limit_cents: Number(cfgCostCents),
            max_concurrency: Number(cfgConcurrency),
            schedule_cron: cfgCron,
            reason: 'Updated via Agent Detail Workspace Configuration tab',
          },
        },
      );
      setAgent(res.agent);
      onNotify(`Saved configuration v${res.agent.version} · Audit ${res.audit_id}`, 'success');
      onAgentMutated();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Configuration save failed', 'error');
    } finally {
      setCfgSaving(false);
    }
  };

  const handleApprovalDecision = async (approvalId: string, decision: 'approved' | 'rejected') => {
    try {
      const res = await apiRequest<{ audit_id: string }>(`/approvals/${approvalId}/decide`, {
        method: 'POST',
        body: { decision, note: `${decision} from Agent Detail Governance tab` },
      });
      onNotify(`Approval ${approvalId} ${decision} · Audit ${res.audit_id}`, 'success');
      onAgentMutated();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Approval decision failed', 'error');
    }
  };

  if (!agentId) {
    return (
      <section className="flex h-full min-h-[340px] items-center justify-center rounded-xl border border-slate-200 bg-white p-6 text-center text-xs text-slate-500">
        Select an agent row in the Workforce Command Center to inspect its canonical workspace.
      </section>
    );
  }

  return (
    <section aria-label="Agent Detail Workspace" className="flex flex-col rounded-xl border border-slate-200 bg-white">
      {/* Section Title Bar */}
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex h-6 items-center rounded bg-blue-600 px-2 font-mono text-xs font-semibold text-white">
            02
          </span>
          <h2 className="text-base font-bold text-slate-900">Agent Detail Workspace</h2>
        </div>
        {agent && (
          <span className="font-mono text-[11px] text-slate-500">
            Canonical v{agent.version} · {agent.config_version}
          </span>
        )}
      </div>

      {/* Persistent Agent Header */}
      {agent && (
        <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-4 pb-3">
          <div>
            <div className="flex flex-wrap items-center gap-2.5">
              <h3 className="text-lg font-bold text-slate-900">{agent.display_name}</h3>
              <AgentStatusChip status={agent.status} />
              <AgentHealthChip health={agent.health} />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
              <span className="font-mono font-medium text-slate-700">{agent.agent_id}</span>
              <span>·</span>
              <span>{agent.role} specialist</span>
              <span>·</span>
              <span>Supervisor: <strong className="font-medium text-slate-800">{agent.owner_name}</strong></span>
              <span>·</span>
              <span>Team: {agent.team_name}</span>
            </div>
          </div>

          <div className="relative flex items-center gap-2">
            {agent.status === 'running' ? (
              <button
                type="button"
                disabled={cmdBusy}
                onClick={() => sendCommand('pause', 'Operator paused via Agent Detail Workspace')}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                <Pause className="h-3.5 w-3.5" /> Pause
              </button>
            ) : (
              <button
                type="button"
                disabled={cmdBusy}
                onClick={() => sendCommand('resume', 'Operator resumed via Agent Detail Workspace')}
                className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" /> Resume
              </button>
            )}
            <button
              type="button"
              disabled={cmdBusy || agent.status === 'idle' || agent.status === 'disabled'}
              onClick={() => sendCommand('safe_stop', 'Cooperative safe stop with checkpoint')}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <Shield className="h-3.5 w-3.5" /> Safe stop
            </button>
            <button
              type="button"
              onClick={() => setMoreOpen((v) => !v)}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              More <ChevronDown className="h-3.5 w-3.5" />
            </button>

            {moreOpen && (
              <div className="absolute right-0 top-9 z-30 w-44 rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
                <button
                  type="button"
                  onClick={() => sendCommand('retry', 'Retry active workflow after remediation')}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-slate-50"
                >
                  <RotateCcw className="h-3.5 w-3.5" /> Retry task
                </button>
                <button
                  type="button"
                  onClick={() => sendCommand('request_approval', 'Escalated to supervisor approval gate')}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-violet-700 hover:bg-violet-50"
                >
                  <ShieldCheck className="h-3.5 w-3.5" /> Request approval
                </button>
                <button
                  type="button"
                  onClick={() => sendCommand('cancel', 'Cancelled active task with checkpoint')}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-amber-700 hover:bg-amber-50"
                >
                  Cancel current task
                </button>
                <button
                  type="button"
                  onClick={() => sendCommand('rollback', 'Rolled back to last verified release snapshot')}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-slate-50"
                >
                  Rollback checkpoint
                </button>
                <div className="my-1 border-t border-slate-100" />
                <button
                  type="button"
                  onClick={() => sendCommand('disable', 'Supervisor disabled agent')}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs font-medium text-rose-700 hover:bg-rose-50"
                >
                  <ShieldAlert className="h-3.5 w-3.5" /> Disable agent
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 10 Professional Tabs */}
      <div className="flex overflow-x-auto border-b border-slate-200 px-5">
        {TABS.map((t) => {
          const isActive = activeTab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setActiveTab(t.id)}
              className={`border-b-2 px-2.5 py-2 text-xs font-medium transition-colors whitespace-nowrap ${
                isActive
                  ? 'border-blue-600 text-blue-700 font-semibold'
                  : 'border-transparent text-slate-600 hover:text-slate-900'
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab Body */}
      <div className="flex-1 p-5 text-xs">
        {loading && !tabData && activeTab !== 'configuration' ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3 animate-pulse">
            <div className="h-36 rounded-lg bg-slate-100" />
            <div className="h-36 rounded-lg bg-slate-100" />
            <div className="h-36 rounded-lg bg-slate-100" />
          </div>
        ) : error ? (
          <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-rose-800">{error}</div>
        ) : activeTab === 'overview' && tabData ? (
          <OverviewTab
            data={tabData as unknown as OverviewPayload}
            onSelectAgent={onSelectAgent}
            onOpenTrace={() => setActiveTab('activity')}
          />
        ) : activeTab === 'tasks' && tabData ? (
          <TasksTab
            items={(tabData.items as TaskRecord[]) ?? []}
            onRefresh={onAgentMutated}
            onNotify={onNotify}
          />
        ) : activeTab === 'skills-tools' && tabData ? (
          <SkillsToolsTab data={tabData as unknown as SkillsToolsPayload} />
        ) : activeTab === 'model-usage' && tabData ? (
          <ModelUsageTab data={tabData as unknown as ModelUsagePayload} />
        ) : activeTab === 'memory' && tabData ? (
          <MemoryTab data={tabData as unknown as MemoryPayload} />
        ) : activeTab === 'activity' && tabData ? (
          <ActivityTab items={(tabData.items as ActivityEvent[]) ?? []} />
        ) : activeTab === 'quality' && tabData ? (
          <QualityTab data={tabData as unknown as QualityPayload} />
        ) : activeTab === 'incidents' && tabData ? (
          <IncidentsTab items={(tabData.items as IncidentRecord[]) ?? []} />
        ) : activeTab === 'governance' && tabData ? (
          <GovernanceTab data={tabData as unknown as GovernancePayload} onDecide={handleApprovalDecision} />
        ) : activeTab === 'configuration' && agent ? (
          <form onSubmit={handleSaveConfig} className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 font-semibold text-slate-900">
                <Sliders className="h-3.5 w-3.5 text-blue-600" />
                Runtime Limits & Model Configuration (Optimistic Lock v{agent.version})
              </div>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div>
                <label className="mb-1 block text-slate-600">Primary Model</label>
                <input value={cfgModel} onChange={(e) => setCfgModel(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-xs" />
              </div>
              <div>
                <label className="mb-1 block text-slate-600">Fallback Model</label>
                <input value={cfgFallback} onChange={(e) => setCfgFallback(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-xs" />
              </div>
              <div>
                <label className="mb-1 block text-slate-600">Reasoning Mode</label>
                <select value={cfgReasoning} onChange={(e) => setCfgReasoning(e.target.value as 'fast' | 'balanced' | 'deep')} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5">
                  <option value="fast">Fast</option>
                  <option value="balanced">Balanced</option>
                  <option value="deep">Deep</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block text-slate-600">RAM Budget (MB)</label>
                <input type="number" min={128} max={16384} value={cfgRam} onChange={(e) => setCfgRam(Number(e.target.value))} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono tabular-nums" />
              </div>
              <div>
                <label className="mb-1 block text-slate-600">Daily Cost Limit (cents)</label>
                <input type="number" min={0} value={cfgCostCents} onChange={(e) => setCfgCostCents(Number(e.target.value))} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono tabular-nums" />
              </div>
              <div>
                <label className="mb-1 block text-slate-600">Schedule (Cron)</label>
                <input value={cfgCron} onChange={(e) => setCfgCron(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono" />
              </div>
            </div>
            <div className="flex items-center justify-between pt-1">
              <span className="text-[11px] text-slate-500">
                Changes are version-checked against v{agent.version} and recorded in the audit log.
              </span>
              <button
                type="submit"
                disabled={cfgSaving}
                className="rounded-lg bg-blue-600 px-3.5 py-1.5 font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {cfgSaving ? 'Saving...' : 'Save configuration'}
              </button>
            </div>
          </form>
        ) : null}
      </div>
    </section>
  );
}

// ── Tab Sub-views ─────────────────────────────────────────────────────────────

interface OverviewPayload {
  assignment: {
    current_task_id: string | null;
    current_task_title: string | null;
    parent_goal: string | null;
    progress_pct: number;
    next_action: string;
    blocker: string | null;
    handoff_to: { agent_id: string; display_name: string; role: string } | null;
  };
  capabilities_runtime: {
    tools: string[];
    provider: string;
    model: string;
    ram_mb: number | null;
    gpu_vram_mb: number | null;
    telemetry_available: boolean;
    permissions_summary: string;
  };
  recent_activity: ActivityEvent[];
}

function OverviewTab({
  data,
  onSelectAgent,
  onOpenTrace,
}: {
  data: OverviewPayload;
  onSelectAgent: (id: string) => void;
  onOpenTrace: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {/* Assignment */}
        <div className="rounded-lg border border-slate-200 bg-slate-50/40 p-3.5">
          <div className="mb-2.5 font-semibold text-slate-900">Assignment</div>
          <dl className="space-y-2">
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Current task</dt>
              <dd className="text-right font-medium text-slate-900">{data.assignment.current_task_title || '—'}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Parent goal</dt>
              <dd className="text-right text-slate-800">{data.assignment.parent_goal || '—'}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Handoff</dt>
              <dd className="text-right">
                {data.assignment.handoff_to ? (
                  <button
                    type="button"
                    onClick={() => onSelectAgent(data.assignment.handoff_to!.agent_id)}
                    className="font-semibold text-blue-600 hover:underline"
                  >
                    {data.assignment.handoff_to.display_name}
                  </button>
                ) : (
                  <span className="text-slate-400">None</span>
                )}
              </dd>
            </div>
            {data.assignment.blocker && (
              <div className="mt-2 rounded border border-rose-200 bg-rose-50 p-2 text-[11px] text-rose-800">
                {data.assignment.blocker}
              </div>
            )}
          </dl>
        </div>

        {/* Capabilities & Runtime */}
        <div className="rounded-lg border border-slate-200 bg-slate-50/40 p-3.5">
          <div className="mb-2.5 font-semibold text-slate-900">Capabilities &amp; Runtime</div>
          <dl className="space-y-2">
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Tools</dt>
              <dd className="text-right font-medium text-slate-900">{data.capabilities_runtime.tools.join(', ') || 'None'}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Provider</dt>
              <dd className="text-right text-slate-800">{data.capabilities_runtime.provider}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Model</dt>
              <dd className="text-right font-mono text-slate-800">{data.capabilities_runtime.model}</dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">RAM</dt>
              <dd className="text-right font-mono text-slate-800 tabular-nums">
                {data.capabilities_runtime.telemetry_available
                  ? `${data.capabilities_runtime.ram_mb ?? 0} MB · GPU ${data.capabilities_runtime.gpu_vram_mb ? `${data.capabilities_runtime.gpu_vram_mb} MB` : 'N/A'}`
                  : 'Telemetry stale'}
              </dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-500">Permissions</dt>
              <dd className="text-right text-slate-800">{data.capabilities_runtime.permissions_summary}</dd>
            </div>
          </dl>
        </div>

        {/* Recent activity */}
        <div className="rounded-lg border border-slate-200 bg-slate-50/40 p-3.5">
          <div className="mb-2.5 font-semibold text-slate-900">Recent activity</div>
          {data.recent_activity.length === 0 ? (
            <div className="text-slate-500">No recent activity recorded.</div>
          ) : (
            <ul className="space-y-2.5">
              {data.recent_activity.slice(0, 4).map((ev) => (
                <li key={ev.activity_id} className="flex items-start gap-2.5">
                  <span className="mt-0.5 font-mono text-[11px] text-slate-400 tabular-nums">
                    {formatShortTime(ev.occurred_at)}
                  </span>
                  <div className="min-w-0">
                    <div className="font-medium text-slate-800">{ev.action}</div>
                    <div className="truncate text-[11px] text-slate-500">{ev.target}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <button
        type="button"
        onClick={onOpenTrace}
        className="flex w-full items-center justify-between rounded-lg border border-blue-100 bg-blue-50/60 px-3.5 py-2 text-xs font-medium text-blue-700 hover:bg-blue-50 transition-colors"
      >
        <span>View evidence and execution trace</span>
        <ArrowRight className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function TasksTab({
  items,
  onRefresh,
  onNotify,
}: {
  items: TaskRecord[];
  onRefresh: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const attemptComplete = async (task: TaskRecord) => {
    try {
      await apiRequest(`/tasks/${task.task_id}`, {
        method: 'PATCH',
        body: {
          status: 'completed',
          evidence: task.evidence,
          acceptance_checks: task.acceptance_checks,
        },
      });
      onNotify(`Task ${task.task_id} marked COMPLETED with verified evidence.`, 'success');
      onRefresh();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Cannot complete task without passing evidence', 'error');
    }
  };

  if (items.length === 0) return <div className="py-6 text-center text-slate-500">No active tasks assigned to this agent.</div>;
  return (
    <div className="space-y-3">
      {items.map((t) => (
        <div key={t.task_id} className="rounded-lg border border-slate-200 p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <span className="font-mono text-[11px] text-slate-500">{t.task_id}</span>
              <span className="mx-1.5 text-slate-300">·</span>
              <span className="font-semibold text-slate-900">{t.title}</span>
              <span className="mx-1.5 text-slate-300">·</span>
              <span className="uppercase text-[11px] font-medium text-blue-700">{t.status}</span>
            </div>
            {t.status === 'running' && (
              <button
                type="button"
                onClick={() => attemptComplete(t)}
                className="rounded border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
              >
                Verify &amp; complete
              </button>
            )}
          </div>
          <p className="mt-1 text-slate-600">{t.objective}</p>
          <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-slate-500">
            <span>Evidence items: <strong className="font-mono text-slate-800">{t.evidence.length}</strong></span>
            <span>Checks passing: <strong className="font-mono text-slate-800">{t.acceptance_checks.filter((c) => c.passed).length}/{t.acceptance_checks.length}</strong></span>
            <span>Side effects: <strong className="text-slate-800">{t.external_side_effects}</strong></span>
            {t.checkpoint_ref && <span>Checkpoint: <code className="font-mono text-slate-700">{t.checkpoint_ref}</code></span>}
          </div>
        </div>
      ))}
    </div>
  );
}

interface SkillsToolsPayload {
  skills: CanonicalAgent['skills'];
  tools: CanonicalAgent['tools'];
  permissions_summary: string;
}

function SkillsToolsTab({ data }: { data: SkillsToolsPayload }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div className="rounded-lg border border-slate-200 p-3.5">
        <div className="mb-2 font-semibold text-slate-900">Versioned Skills</div>
        <div className="divide-y divide-slate-100">
          {data.skills.map((s) => (
            <div key={s.skill_id} className="flex items-center justify-between py-2">
              <div>
                <div className="font-medium text-slate-800">{s.name}</div>
                <div className="font-mono text-[11px] text-slate-500">v{s.version} · {s.test_status}</div>
              </div>
              <span className="text-[11px] font-medium text-emerald-700">{s.enabled ? 'Enabled' : 'Disabled'}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="rounded-lg border border-slate-200 p-3.5">
        <div className="mb-2 font-semibold text-slate-900">Authorized Tool Bindings</div>
        <div className="divide-y divide-slate-100">
          {data.tools.map((t) => (
            <div key={t.tool_id} className="flex items-center justify-between py-2">
              <div>
                <div className="font-medium text-slate-800">{t.name}</div>
                <div className="font-mono text-[11px] text-slate-500">{t.scope}</div>
              </div>
              <AgentHealthChip health={t.integration_health} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface ModelUsagePayload {
  provider: string;
  model: string;
  endpoint_region: string;
  reasoning_mode: string;
  context_used: number;
  context_limit: number;
  context_utilization_pct: number;
  fallback_model: string;
  tokens_today: number;
  max_tokens_per_day: number;
  cost_today_formatted: string;
  daily_cost_limit_formatted: string;
  quota_remaining_pct: number;
  avg_latency_ms: number;
}

function ModelUsageTab({ data }: { data: ModelUsagePayload }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div className="rounded-lg border border-slate-200 p-3">
        <div className="text-[11px] text-slate-500">Active Model</div>
        <div className="mt-1 font-mono font-semibold text-slate-900">{data.model}</div>
        <div className="text-[11px] text-slate-500">{data.provider} · {data.endpoint_region}</div>
      </div>
      <div className="rounded-lg border border-slate-200 p-3">
        <div className="text-[11px] text-slate-500">Context Window</div>
        <div className="mt-1 font-mono font-semibold text-slate-900 tabular-nums">{data.context_utilization_pct}% used</div>
        <div className="text-[11px] text-slate-500 tabular-nums">{data.context_used.toLocaleString()} / {data.context_limit.toLocaleString()}</div>
      </div>
      <div className="rounded-lg border border-slate-200 p-3">
        <div className="text-[11px] text-slate-500">Spend / Daily Budget</div>
        <div className="mt-1 font-mono font-semibold text-slate-900 tabular-nums">{data.cost_today_formatted}</div>
        <div className="text-[11px] text-slate-500 tabular-nums">Limit {data.daily_cost_limit_formatted} · Quota {data.quota_remaining_pct}%</div>
      </div>
      <div className="rounded-lg border border-slate-200 p-3">
        <div className="text-[11px] text-slate-500">Fallback &amp; Latency</div>
        <div className="mt-1 font-mono font-semibold text-slate-900">{data.fallback_model}</div>
        <div className="text-[11px] text-slate-500 tabular-nums">Avg {data.avg_latency_ms}ms · {data.reasoning_mode}</div>
      </div>
    </div>
  );
}

interface MemoryPayload {
  policy: CanonicalAgent['memory_policy'];
  items: MemoryItem[];
  omitted_restricted_count: number;
}

function MemoryTab({ data }: { data: MemoryPayload }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-slate-50/70 px-3.5 py-2 text-[11px] text-slate-600">
        <span>
          Retention: <strong className="text-slate-900">{data.policy.retention_days}d</strong> · Provenance: <strong className="text-slate-900">{data.policy.provenance_status}</strong> · Redaction: <strong className="text-slate-900">{data.policy.redaction_enabled ? 'Enforced' : 'Off'}</strong>
        </span>
        {data.omitted_restricted_count > 0 && (
          <span className="font-medium text-amber-700">
            {data.omitted_restricted_count} restricted record(s) hidden for current role
          </span>
        )}
      </div>
      {data.items.length === 0 ? (
        <div className="py-5 text-center text-slate-500">No memory records visible in this permission scope.</div>
      ) : (
        data.items.map((m) => (
          <div key={m.memory_id} className="rounded-lg border border-slate-200 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold text-slate-900">{m.title}</div>
              <div className="font-mono text-[11px] text-slate-500">
                {m.layer} · {m.provenance_type} · conf {Math.round(m.confidence * 100)}%
              </div>
            </div>
            <p className="mt-1 text-slate-700">{m.content}</p>
            {m.competing_claim && (
              <div className="mt-1.5 rounded border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-900">
                Competing claim preserved: {m.competing_claim}
              </div>
            )}
            <div className="mt-1.5 font-mono text-[11px] text-slate-400">
              Citations: {m.citations.join(', ')} · {m.sensitivity} · {m.correlation_id}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function ActivityTab({ items }: { items: ActivityEvent[] }) {
  if (items.length === 0) return <div className="py-6 text-center text-slate-500">No correlated activity events yet.</div>;
  return (
    <div className="divide-y divide-slate-100">
      {items.map((ev) => (
        <div key={ev.activity_id} className="flex items-start justify-between gap-3 py-2.5">
          <div>
            <div className="font-medium text-slate-900">
              {ev.action} <span className="font-normal text-slate-500">— {ev.target}</span>
            </div>
            <div className="mt-0.5 text-slate-600">{ev.detail}</div>
            <div className="mt-0.5 font-mono text-[11px] text-slate-400">
              Actor: {ev.actor_name} · Correlation: {ev.correlation_id}
            </div>
          </div>
          <span className="shrink-0 font-mono text-[11px] text-slate-500 tabular-nums">
            {formatShortTime(ev.occurred_at)}
          </span>
        </div>
      ))}
    </div>
  );
}

interface QualityPayload {
  success_rate_pct: number;
  quality_score_pct: number;
  tasks_completed: number;
  tasks_failed: number;
  sla_compliance_pct: number;
  evaluations: QualityEvaluation[];
}

function QualityTab({ data }: { data: QualityPayload }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-3">
        <div className="rounded-lg border border-slate-200 p-2.5">
          <div className="text-[11px] text-slate-500">Success rate</div>
          <div className="font-mono text-base font-bold text-slate-900 tabular-nums">{data.success_rate_pct}%</div>
        </div>
        <div className="rounded-lg border border-slate-200 p-2.5">
          <div className="text-[11px] text-slate-500">Quality score</div>
          <div className="font-mono text-base font-bold text-slate-900 tabular-nums">{data.quality_score_pct}%</div>
        </div>
        <div className="rounded-lg border border-slate-200 p-2.5">
          <div className="text-[11px] text-slate-500">Completed / Failed</div>
          <div className="font-mono text-base font-bold text-slate-900 tabular-nums">{data.tasks_completed} / {data.tasks_failed}</div>
        </div>
        <div className="rounded-lg border border-slate-200 p-2.5">
          <div className="text-[11px] text-slate-500">SLA compliance</div>
          <div className="font-mono text-base font-bold text-slate-900 tabular-nums">{data.sla_compliance_pct}%</div>
        </div>
      </div>
      {data.evaluations.length === 0 ? (
        <div className="py-3 text-slate-500">No dedicated evaluation suites recorded yet.</div>
      ) : (
        <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {data.evaluations.map((ev) => (
            <div key={ev.eval_id} className="flex items-center justify-between px-3.5 py-2.5">
              <div>
                <div className="font-medium text-slate-900">{ev.suite}</div>
                <div className="text-[11px] text-slate-500">
                  Passed {ev.passed} · Failed {ev.failed} · Human corrections {ev.human_corrections}
                </div>
              </div>
              <div className="font-mono font-bold text-emerald-700 tabular-nums">{ev.score_pct}%</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function IncidentsTab({ items }: { items: IncidentRecord[] }) {
  if (items.length === 0) return <div className="py-6 text-center text-slate-500">Zero incidents recorded for this agent.</div>;
  return (
    <div className="space-y-2.5">
      {items.map((inc) => (
        <div key={inc.incident_id} className="rounded-lg border border-slate-200 p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="font-semibold text-slate-900">
              <span className="font-mono text-slate-500 mr-1.5">{inc.incident_id}</span>
              {inc.title}
            </div>
            <span className="font-mono text-[11px] uppercase font-medium text-amber-800">{inc.status}</span>
          </div>
          <p className="mt-1 text-slate-600"><strong>Root cause:</strong> {inc.root_cause}</p>
          <p className="mt-0.5 text-slate-600"><strong>Impact:</strong> {inc.impact}</p>
          <div className="mt-1.5 font-mono text-[11px] text-slate-500">
            Rollback point: {inc.rollback_point}{inc.regression_test ? ` · Regression: ${inc.regression_test}` : ''}
          </div>
        </div>
      ))}
    </div>
  );
}

interface GovernancePayload {
  owner_name: string;
  risk_level: CanonicalAgent['risk_level'];
  data_sensitivity: string;
  audit_status: string;
  permissions_summary: string;
  approvals: ApprovalRecord[];
  audit_trail: AuditEvent[];
}

function GovernanceTab({
  data,
  onDecide,
}: {
  data: GovernancePayload;
  onDecide: (approvalId: string, decision: 'approved' | 'rejected') => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4 rounded-lg border border-slate-200 bg-slate-50/70 px-3.5 py-2">
        <span>Owner: <strong className="text-slate-900">{data.owner_name}</strong></span>
        <span>Risk: <RiskText risk={data.risk_level} /></span>
        <span>Data sensitivity: <strong className="capitalize text-slate-900">{data.data_sensitivity}</strong></span>
        <span>Scope: <strong className="text-slate-900">{data.permissions_summary}</strong></span>
      </div>

      {data.approvals.length > 0 && (
        <div className="space-y-2">
          <div className="font-semibold text-slate-900">Approval Gates</div>
          {data.approvals.map((apr) => (
            <div key={apr.approval_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-violet-200 bg-violet-50/40 p-3">
              <div>
                <div className="font-semibold text-slate-900">{apr.action_requested}</div>
                <div className="text-[11px] text-slate-600">{apr.reason} · Status: <strong>{apr.status}</strong></div>
              </div>
              {apr.status === 'pending' && (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => onDecide(apr.approval_id, 'approved')}
                    className="rounded-lg bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-700"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    onClick={() => onDecide(apr.approval_id, 'rejected')}
                    className="rounded-lg border border-rose-200 bg-white px-3 py-1 text-xs font-semibold text-rose-700 hover:bg-rose-50"
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div>
        <div className="mb-1.5 font-semibold text-slate-900">Recent Audit Trail</div>
        {data.audit_trail.length === 0 ? (
          <div className="text-slate-500">No audit entries recorded for this agent yet.</div>
        ) : (
          <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
            {data.audit_trail.slice(0, 5).map((a) => (
              <div key={a.audit_id} className="flex items-center justify-between gap-2 px-3 py-2 text-[11px]">
                <div>
                  <span className="font-mono font-semibold text-slate-800">{a.action}</span>
                  <span className="mx-1.5 text-slate-300">·</span>
                  <span className="text-slate-600">{a.reason}</span>
                </div>
                <span className="font-mono text-slate-400">{a.actor_name} · {formatShortTime(a.occurred_at)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
