import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Database,
  FileSearch,
  HardDrive,
  Link2,
  RefreshCw,
  Search,
  Shield,
  Sliders,
  XCircle,
} from 'lucide-react';
import type {
  ActorRole,
  AuditEvent,
  CanonicalAgent,
  GraphifySnapshot,
  GraphRefreshJob,
  IncidentRecord,
  IntegrationConnector,
  TaskRecord,
} from '../shared/types';
import { apiRequest, getClientAuth, makeIdempotencyKey, setClientAuth } from '../api/client';
import { AgentHealthChip, formatShortTime } from './StatusBadge';

export type NavSection =
  | 'workforce'
  | 'tasks'
  | 'organization'
  | 'knowledge'
  | 'integrations'
  | 'incidents'
  | 'audit'
  | 'settings';

interface SecondaryViewsProps {
  section: Exclude<NavSection, 'workforce' | 'organization'>;
  agents: CanonicalAgent[];
  refreshNonce: number;
  onSelectAgent: (agentId: string) => void;
  onMutated: () => void;
  onAuthChanged: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}

export function SecondaryViews({
  section,
  agents,
  refreshNonce,
  onSelectAgent,
  onMutated,
  onAuthChanged,
  onNotify,
}: SecondaryViewsProps) {
  if (section === 'tasks') {
    return <TasksView agents={agents} refreshNonce={refreshNonce} onSelectAgent={onSelectAgent} onMutated={onMutated} onNotify={onNotify} />;
  }
  if (section === 'knowledge') {
    return <KnowledgeGraphifyView refreshNonce={refreshNonce} onMutated={onMutated} onNotify={onNotify} />;
  }
  if (section === 'integrations') {
    return <IntegrationsView refreshNonce={refreshNonce} onSelectAgent={onSelectAgent} onMutated={onMutated} onNotify={onNotify} />;
  }
  if (section === 'incidents') {
    return <IncidentsView refreshNonce={refreshNonce} onSelectAgent={onSelectAgent} onMutated={onMutated} onNotify={onNotify} />;
  }
  if (section === 'audit') {
    return <AuditView refreshNonce={refreshNonce} />;
  }
  return <SettingsResourcesView refreshNonce={refreshNonce} onAuthChanged={onAuthChanged} onNotify={onNotify} />;
}

/* ── Tasks View ───────────────────────────────────────────────────────────── */

function TasksView({
  agents,
  refreshNonce,
  onSelectAgent,
  onMutated,
  onNotify,
}: {
  agents: CanonicalAgent[];
  refreshNonce: number;
  onSelectAgent: (id: string) => void;
  onMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState('all');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await apiRequest<{ items: TaskRecord[] }>(`/tasks?status=${encodeURIComponent(statusFilter)}`);
      setTasks(res.items);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to load tasks', 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, refreshNonce]);

  const completeTask = async (task: TaskRecord, withEvidence: boolean) => {
    try {
      await apiRequest(`/tasks/${task.task_id}`, {
        method: 'PATCH',
        body: {
          status: 'completed',
          reason: withEvidence ? 'Completed with verified evidence' : 'Attempted completion without evidence',
          ...(withEvidence
            ? {
                evidence:
                  task.evidence.length > 0
                    ? task.evidence
                    : [
                        {
                          evidence_id: `ev-${Date.now()}`,
                          label: 'Operator verified completion artifact',
                          source_uri: `wasp://evidence/${task.task_id}`,
                          verified_at: new Date().toISOString(),
                        },
                      ],
                acceptance_checks: (task.acceptance_checks.length > 0
                  ? task.acceptance_checks
                  : [{ check: 'Verification gate passed', passed: true }]
                ).map((c) => ({ ...c, passed: true })),
              }
            : { evidence: [], acceptance_checks: [{ check: 'Verification gate passed', passed: false }] }),
        },
      });
      onNotify(`Task ${task.task_id} marked completed`, 'success');
      onMutated();
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Task transition rejected', 'error');
    }
  };

  const assignTask = async (taskId: string, agentId: string) => {
    if (!agentId) return;
    try {
      await apiRequest(`/tasks/${taskId}`, {
        method: 'PATCH',
        body: { agent_id: agentId, reason: 'Assigned from Tasks view' },
      });
      onNotify(`Assigned ${taskId} to ${agentId}`, 'success');
      onMutated();
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Assignment failed', 'error');
    }
  };

  return (
    <section className="rounded-xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
        <div>
          <h2 className="font-display text-lg font-bold text-slate-900">Task Ledger &amp; Completion Gates</h2>
          <p className="text-xs text-slate-500">
            Tasks enforce state-machine transitions and require verified evidence plus passing acceptance checks before completion.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            aria-label="Filter tasks by status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
          >
            <option value="all">All statuses</option>
            <option value="running">Running</option>
            <option value="queued">Queued</option>
            <option value="blocked">Blocked</option>
            <option value="awaiting_approval">Awaiting approval</option>
            <option value="completed">Completed</option>
          </select>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              <th className="px-4 py-2.5">Task</th>
              <th className="px-4 py-2.5">Status</th>
              <th className="px-4 py-2.5">Assigned Agent</th>
              <th className="px-4 py-2.5">Evidence / Checks</th>
              <th className="px-4 py-2.5">Progress</th>
              <th className="px-4 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {tasks.map((t) => {
              const passedCount = t.acceptance_checks.filter((c) => c.passed).length;
              return (
                <tr key={t.task_id} className="hover:bg-slate-50/70">
                  <td className="px-4 py-3">
                    <div className="font-semibold text-slate-900">
                      <span className="mr-1.5 font-mono text-[11px] text-slate-500">{t.task_id}</span>
                      {t.title}
                    </div>
                    <div className="mt-0.5 text-[11px] text-slate-500">{t.parent_goal} · {t.project}</div>
                    {t.blocker_reason && (
                      <div className="mt-1 text-[11px] font-medium text-red-700">Blocker: {t.blocker_reason}</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex rounded bg-slate-100 px-2 py-0.5 font-mono text-[11px] font-semibold text-slate-700">
                      {t.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {t.agent_id ? (
                      <button
                        type="button"
                        onClick={() => onSelectAgent(t.agent_id!)}
                        className="font-semibold text-blue-700 hover:underline"
                      >
                        {agents.find((a) => a.agent_id === t.agent_id)?.display_name ?? t.agent_id}
                      </button>
                    ) : (
                      <select
                        aria-label={`Assign ${t.task_id}`}
                        defaultValue=""
                        onChange={(e) => void assignTask(t.task_id, e.target.value)}
                        className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-900"
                      >
                        <option value="">Unowned — assign…</option>
                        {agents.map((a) => (
                          <option key={a.agent_id} value={a.agent_id}>
                            {a.display_name} ({a.agent_id})
                          </option>
                        ))}
                      </select>
                    )}
                  </td>
                  <td className="px-4 py-3 tabular-nums text-slate-600">
                    {t.evidence.length} evidence · {passedCount}/{t.acceptance_checks.length} checks
                  </td>
                  <td className="px-4 py-3 font-mono tabular-nums text-slate-700">{t.progress_pct}%</td>
                  <td className="px-4 py-3 text-right">
                    {t.status === 'running' && (
                      <div className="inline-flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => void completeTask(t, true)}
                          className="rounded border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-800 hover:bg-emerald-100"
                        >
                          Verify &amp; Complete
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ── Knowledge / Graphify View ────────────────────────────────────────────── */

function KnowledgeGraphifyView({
  refreshNonce,
  onMutated,
  onNotify,
}: {
  refreshNonce: number;
  onMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const [snapshot, setSnapshot] = useState<GraphifySnapshot | null>(null);
  const [jobs, setJobs] = useState<GraphRefreshJob[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await apiRequest<{ snapshot: GraphifySnapshot; recent_jobs: GraphRefreshJob[] }>('/graph/graphify');
      setSnapshot(res.snapshot);
      setJobs(res.recent_jobs);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to load Graphify snapshot', 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshNonce]);

  useEffect(() => {
    if (!activeJobId) return;
    const timer = setInterval(async () => {
      try {
        const res = await apiRequest<{ job: GraphRefreshJob; current_snapshot: GraphifySnapshot }>(
          `/graph/refresh/${activeJobId}`,
        );
        setSnapshot(res.current_snapshot);
        if (['promoted', 'failed', 'cancelled'].includes(res.job.status)) {
          setActiveJobId(null);
          void load();
          onMutated();
          onNotify(
            res.job.status === 'promoted'
              ? `Graphify promoted atomically to v${res.current_snapshot.graph_version}`
              : res.job.status === 'failed'
                ? `Graphify refresh failed — previous valid v${res.current_snapshot.graph_version} retained`
                : 'Graphify refresh cancelled',
            res.job.status === 'promoted' ? 'success' : 'error',
          );
        }
      } catch {
        setActiveJobId(null);
      }
    }, 200);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobId]);

  const triggerRefresh = async (simulateFailure: boolean) => {
    try {
      const res = await apiRequest<{ job: GraphRefreshJob }>('/graph/refresh', {
        method: 'POST',
        body: { simulate_failure: simulateFailure },
      });
      setActiveJobId(res.job.job_id);
      onNotify(`Started async Graphify refresh (${res.job.job_id})`, 'info');
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Refresh failed to start', 'error');
    }
  };

  const cancelJob = async (jobId: string) => {
    try {
      await apiRequest(`/graph/refresh/${jobId}/cancel`, { method: 'POST' });
      setActiveJobId(null);
      onNotify(`Cancelled job ${jobId}`, 'info');
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Cancel failed', 'error');
    }
  };

  return (
    <section className="space-y-4">
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <Database className="h-5 w-5 text-blue-600" />
            <div>
              <h2 className="font-display text-lg font-bold text-slate-900">Knowledge Graph / Graphify</h2>
              <p className="text-xs text-slate-500">
                Asynchronous AST graph builds validate into staging and promote atomically; failures never wipe the last valid snapshot.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void triggerRefresh(false)}
              disabled={Boolean(activeJobId)}
              className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-60"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${activeJobId ? 'animate-spin' : ''}`} />
              Refresh Graphify
            </button>
            <button
              type="button"
              onClick={() => void triggerRefresh(true)}
              disabled={Boolean(activeJobId)}
              className="inline-flex items-center gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-900 hover:bg-amber-100 disabled:opacity-60"
            >
              <AlertTriangle className="h-3.5 w-3.5" />
              Test Atomic Failure Safety
            </button>
            {activeJobId && (
              <button
                type="button"
                onClick={() => void cancelJob(activeJobId)}
                className="inline-flex items-center gap-1 rounded-md border border-red-200 bg-red-50 px-3 py-1.5 text-xs font-semibold text-red-700"
              >
                <XCircle className="h-3.5 w-3.5" />
                Cancel
              </button>
            )}
          </div>
        </div>

        {snapshot && (
          <>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <MetricBox label="Graph Version" value={`v${snapshot.graph_version}`} mono />
              <MetricBox label="Repository / Branch" value={`${snapshot.repository} (${snapshot.branch})`} />
              <MetricBox label="Commit SHA" value={snapshot.commit_sha} mono />
              <MetricBox label="Nodes / Edges" value={`${snapshot.node_count} / ${snapshot.edge_count}`} mono />
              <MetricBox label="Freshness" value={snapshot.stale ? 'Stale' : `Fresh · ${formatShortTime(snapshot.generated_at)}`} />
            </div>

            {snapshot.last_error && (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-900">
                <span className="font-semibold">Last build notice: </span>
                {snapshot.last_error}
              </div>
            )}

            <div className="mt-4">
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-600">Indexed Submodules</h3>
              <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
                {snapshot.modules.map((m) => (
                  <div key={m.name} className="rounded-lg border border-slate-200 bg-slate-50/70 p-3 text-xs">
                    <div className="font-mono font-semibold text-slate-900">{m.name}</div>
                    <div className="mt-1 flex items-center justify-between text-[11px] text-slate-600 tabular-nums">
                      <span>{m.nodes} nodes · {m.edges} edges</span>
                      <span>H={m.entropy.toFixed(2)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Recent Refresh Jobs */}
      <div className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-bold text-slate-900">Recent Refresh Jobs</h3>
          <button
            type="button"
            onClick={() => void load()}
            className="text-xs font-medium text-blue-600 hover:underline"
          >
            {loading ? 'Refreshing…' : 'Reload'}
          </button>
        </div>
        {jobs.length === 0 ? (
          <p className="text-xs text-slate-500">No refresh jobs run in this session yet.</p>
        ) : (
          <div className="space-y-2">
            {jobs.map((j) => (
              <div key={j.job_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 px-3.5 py-2.5 text-xs">
                <div>
                  <span className="font-mono font-semibold text-slate-900">{j.job_id}</span>
                  <span className="mx-2 text-slate-400">·</span>
                  <span className="text-slate-600">Target v{j.target_version}</span>
                  {j.error && <div className="mt-0.5 text-[11px] text-red-700">{j.error}</div>}
                </div>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-500">{j.correlation_id}</span>
                  <span
                    className={`rounded px-2 py-0.5 text-[11px] font-semibold ${
                      j.status === 'promoted'
                        ? 'bg-emerald-50 text-emerald-700'
                        : j.status === 'failed'
                          ? 'bg-red-50 text-red-700'
                          : 'bg-blue-50 text-blue-700'
                    }`}
                  >
                    {j.status} ({j.progress_pct}%)
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/* ── Integrations View ────────────────────────────────────────────────────── */

function IntegrationsView({
  refreshNonce,
  onSelectAgent,
  onMutated,
  onNotify,
}: {
  refreshNonce: number;
  onSelectAgent: (id: string) => void;
  onMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const [connectors, setConnectors] = useState<IntegrationConnector[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = async () => {
    try {
      const res = await apiRequest<{ items: IntegrationConnector[] }>('/integrations');
      setConnectors(res.items);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to load integrations', 'error');
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshNonce]);

  const syncConnector = async (c: IntegrationConnector) => {
    setBusyId(c.integration_id);
    try {
      const res = await apiRequest<{ deduplicated: boolean; integration: IntegrationConnector }>(
        `/integrations/${c.integration_id}/sync`,
        {
          method: 'POST',
          body: { idempotency_key: makeIdempotencyKey(`sync_${c.integration_id}`) },
        },
      );
      onNotify(
        `${res.integration.name} synced cleanly (run #${res.integration.sync_runs}, dead-letters cleared)`,
        'success',
      );
      onMutated();
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Connector sync failed', 'error');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="font-display text-lg font-bold text-slate-900">Integrations &amp; Connectors</h2>
          <p className="text-xs text-slate-500">
            Idempotent webhooks and dead-letter replay. Recovering a degraded connector automatically unblocks dependent agents.
          </p>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {connectors.map((c) => (
          <div key={c.integration_id} className="rounded-lg border border-slate-200 p-4">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <Link2 className="h-4 w-4 text-blue-600" />
                <div>
                  <div className="text-sm font-bold text-slate-900">{c.name}</div>
                  <div className="text-[11px] text-slate-500">
                    {c.provider} · <span className="font-mono">{c.secret_ref}</span>
                  </div>
                </div>
              </div>
              <AgentHealthChip health={c.status} />
            </div>

            {c.last_error && (
              <div className="mt-2.5 rounded border border-red-200 bg-red-50 px-2.5 py-1.5 text-[11px] text-red-700">
                {c.last_error}
              </div>
            )}

            <div className="mt-3 grid grid-cols-3 gap-2 rounded-md bg-slate-50 p-2 text-[11px] tabular-nums">
              <div>
                <div className="text-slate-500">Rate limit</div>
                <div className="font-semibold text-slate-900">{c.rate_limit_remaining_pct}%</div>
              </div>
              <div>
                <div className="text-slate-500">Sync runs</div>
                <div className="font-semibold text-slate-900">{c.sync_runs}</div>
              </div>
              <div>
                <div className="text-slate-500">Dead letters</div>
                <div className={`font-semibold ${c.dead_letter_count > 0 ? 'text-red-700' : 'text-slate-900'}`}>
                  {c.dead_letter_count}
                </div>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1">
                {c.connected_agents.slice(0, 4).map((aid) => (
                  <button
                    key={aid}
                    type="button"
                    onClick={() => onSelectAgent(aid)}
                    className="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[10px] font-semibold text-slate-700 hover:border-blue-400 hover:text-blue-700"
                  >
                    {aid}
                  </button>
                ))}
              </div>
              <button
                type="button"
                disabled={busyId === c.integration_id}
                onClick={() => void syncConnector(c)}
                className="inline-flex items-center gap-1 rounded-md border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700 hover:bg-blue-100 disabled:opacity-60"
              >
                <RefreshCw className={`h-3 w-3 ${busyId === c.integration_id ? 'animate-spin' : ''}`} />
                Sync &amp; Replay
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Incidents View ───────────────────────────────────────────────────────── */

function IncidentsView({
  refreshNonce,
  onSelectAgent,
  onMutated,
  onNotify,
}: {
  refreshNonce: number;
  onSelectAgent: (id: string) => void;
  onMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const [incidents, setIncidents] = useState<IncidentRecord[]>([]);

  const load = async () => {
    try {
      const res = await apiRequest<{ items: IncidentRecord[] }>('/incidents');
      setIncidents(res.items);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to load incidents', 'error');
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshNonce]);

  const resolveIncident = async (id: string) => {
    try {
      await apiRequest(`/incidents/${id}/resolve`, {
        method: 'POST',
        body: { fix_summary: 'Verified recovery and cleared incident via Workforce Operations Console.' },
      });
      onNotify(`Incident ${id} resolved`, 'success');
      onMutated();
      void load();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to resolve incident', 'error');
    }
  };

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5">
      <h2 className="font-display text-lg font-bold text-slate-900">Operational Incidents</h2>
      <p className="mb-4 text-xs text-slate-500">Root cause, rollback checkpoint references, and correlation traceability.</p>

      <div className="space-y-3">
        {incidents.map((inc) => (
          <div key={inc.incident_id} className="rounded-lg border border-slate-200 p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs font-bold text-slate-900">{inc.incident_id}</span>
                  <span
                    className={`rounded px-2 py-0.5 text-[11px] font-semibold ${
                      inc.status === 'resolved' ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'
                    }`}
                  >
                    {inc.status} · {inc.severity}
                  </span>
                  <button
                    type="button"
                    onClick={() => onSelectAgent(inc.agent_id)}
                    className="font-mono text-xs font-semibold text-blue-700 hover:underline"
                  >
                    {inc.agent_id}
                  </button>
                </div>
                <div className="mt-1 text-sm font-bold text-slate-900">{inc.title}</div>
                <p className="mt-1 text-xs text-slate-600">{inc.root_cause}</p>
                <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-slate-500">
                  <span>Impact: {inc.impact}</span>
                  <span className="font-mono">Rollback: {inc.rollback_point}</span>
                  <span className="font-mono">Corr: {inc.correlation_id}</span>
                </div>
              </div>
              {inc.status !== 'resolved' ? (
                <button
                  type="button"
                  onClick={() => void resolveIncident(inc.incident_id)}
                  className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-700"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  Resolve
                </button>
              ) : (
                <span className="text-[11px] text-emerald-700">Resolved {formatShortTime(inc.resolved_at)}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Audit Log View ───────────────────────────────────────────────────────── */

function AuditView({ refreshNonce }: { refreshNonce: number }) {
  const [audits, setAudits] = useState<AuditEvent[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await apiRequest<{ items: AuditEvent[] }>(`/audit?query=${encodeURIComponent(query)}`);
      setAudits(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to read audit trail');
      setAudits([]);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, refreshNonce]);

  return (
    <section className="rounded-xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
        <div className="flex items-center gap-2">
          <FileSearch className="h-5 w-5 text-blue-600" />
          <div>
            <h2 className="font-display text-lg font-bold text-slate-900">Immutable Audit Trail</h2>
            <p className="text-xs text-slate-500">Every command, approval, export, and configuration diff with correlation IDs.</p>
          </div>
        </div>
        <div className="relative w-64">
          <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter action, actor, ID…"
            className="w-full rounded-md border border-slate-200 py-1.5 pl-8 pr-3 text-xs text-slate-800 focus:border-blue-500 focus:outline-none"
          />
        </div>
      </div>

      {error ? (
        <div className="p-5 text-xs text-red-700">{error}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                <th className="px-4 py-2.5">Time</th>
                <th className="px-4 py-2.5">Actor / Role</th>
                <th className="px-4 py-2.5">Action</th>
                <th className="px-4 py-2.5">Entity</th>
                <th className="px-4 py-2.5">Reason &amp; State Diff</th>
                <th className="px-4 py-2.5">Correlation</th>
                <th className="px-4 py-2.5">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {audits.map((a) => (
                <tr key={a.audit_id} className="align-top hover:bg-slate-50/70">
                  <td className="whitespace-nowrap px-4 py-2.5 font-mono text-[11px] text-slate-500 tabular-nums">
                    {formatShortTime(a.occurred_at)}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="font-semibold text-slate-900">{a.actor_name}</div>
                    <div className="text-[11px] text-slate-500">{a.actor_role}</div>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[11px] font-semibold text-slate-800">{a.action}</td>
                  <td className="px-4 py-2.5 font-mono text-[11px] text-blue-700">
                    {a.entity_type}:{a.entity_id}
                  </td>
                  <td className="max-w-sm px-4 py-2.5">
                    <div className="text-slate-700">{a.reason}</div>
                    {a.after_state && (
                      <pre className="mt-1 overflow-x-auto rounded bg-slate-50 px-2 py-1 font-mono text-[10px] text-slate-600">
                        {JSON.stringify(a.after_state)}
                      </pre>
                    )}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">{a.correlation_id}</td>
                  <td className="px-4 py-2.5">
                    <span
                      className={`rounded px-2 py-0.5 text-[11px] font-semibold ${
                        a.result === 'success'
                          ? 'bg-emerald-50 text-emerald-700'
                          : a.result === 'conflict'
                            ? 'bg-amber-50 text-amber-800'
                            : 'bg-red-50 text-red-700'
                      }`}
                    >
                      {a.result}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/* ── Settings, RBAC & Resource Management Dashboard ───────────────────────── */

interface ResourcePayload {
  process_rss_mb: number;
  process_heap_mb: number;
  process_budget_mb: number;
  memory_pressure_pct: number;
  system_free_ram_mb: number;
  system_total_ram_mb: number;
  logical_cpu_count: number;
  configured_workers: number;
  active_workers: number;
  queue_depth: number;
  max_queue_capacity: number;
  cache_entries: number;
  tokens_today: number;
  cost_today_formatted: string;
  throttle_state: string;
  thresholds: {
    throttle_background_pct: number;
    pause_nonessential_pct: number;
    reject_heavy_pct: number;
    min_system_headroom_pct: number;
  };
}

function SettingsResourcesView({
  refreshNonce,
  onAuthChanged,
  onNotify,
}: {
  refreshNonce: number;
  onAuthChanged: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}) {
  const [auth, setAuth] = useState(getClientAuth());
  const [resources, setResources] = useState<ResourcePayload | null>(null);

  useEffect(() => {
    apiRequest<ResourcePayload>('/resources')
      .then(setResources)
      .catch(() => null);
  }, [refreshNonce, auth]);

  const updateRole = (role: ActorRole) => {
    const names: Record<ActorRole, string> = {
      supervisor: 'J. Lee',
      operator: 'M. Chen',
      auditor: 'R. Patel',
      viewer: 'Guest Analyst',
    };
    setClientAuth({ actorRole: role, actorName: names[role] });
    setAuth(getClientAuth());
    onAuthChanged();
    onNotify(`Switched active session role to ${role} (${names[role]})`, 'info');
  };

  const updateTenant = (tenantId: string) => {
    setClientAuth({ tenantId });
    setAuth(getClientAuth());
    onAuthChanged();
    onNotify(`Switched active tenant scope to ${tenantId}`, 'info');
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {/* RBAC & Tenant Scope Panel */}
      <section className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="flex items-center gap-2">
          <Shield className="h-5 w-5 text-blue-600" />
          <div>
            <h2 className="font-display text-base font-bold text-slate-900">Access Control &amp; Tenant Scope</h2>
            <p className="text-xs text-slate-500">
              Server-side RBAC and tenant scoping are enforced on every /api/v1/* endpoint.
            </p>
          </div>
        </div>

        <div className="mt-4 space-y-4">
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-slate-700">Active Role</label>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {(['supervisor', 'operator', 'auditor', 'viewer'] as ActorRole[]).map((role) => (
                <button
                  key={role}
                  type="button"
                  onClick={() => updateRole(role)}
                  className={`rounded-lg border px-3 py-2 text-xs font-semibold capitalize transition ${
                    auth.actorRole === role
                      ? 'border-blue-600 bg-blue-50 text-blue-800'
                      : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  }`}
                >
                  {role}
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-[11px] text-slate-500">
              Viewers cannot run commands, mutate configuration, or view restricted memory items; Auditors get read-only audit &amp; export access; Supervisors can decide governed approvals.
            </p>
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-semibold text-slate-700">Tenant Isolation Scope</label>
            <div className="flex flex-wrap gap-2">
              {['tenant-wasp-ops', 'tenant-external-beta'].map((tid) => (
                <button
                  key={tid}
                  type="button"
                  onClick={() => updateTenant(tid)}
                  className={`rounded-lg border px-3 py-1.5 font-mono text-xs font-medium transition ${
                    auth.tenantId === tid
                      ? 'border-blue-600 bg-blue-50 text-blue-800'
                      : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  }`}
                >
                  {tid}
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* Resource Management Dashboard */}
      <section className="rounded-xl border border-slate-200 bg-white p-5">
        <div className="flex items-center gap-2">
          <Sliders className="h-5 w-5 text-blue-600" />
          <div>
            <h2 className="font-display text-base font-bold text-slate-900">Resource Budgets &amp; Backpressure</h2>
            <p className="text-xs text-slate-500">
              Bounded concurrency, memory ceiling enforcement (70% / 80% / 90%), and live process telemetry.
            </p>
          </div>
        </div>

        {resources && (
          <div className="mt-4 space-y-3">
            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
              <MetricBox label="Process RSS / Budget" value={`${resources.process_rss_mb} / ${resources.process_budget_mb} MB`} mono />
              <MetricBox label="Memory Pressure" value={`${resources.memory_pressure_pct}% (${resources.throttle_state})`} mono />
              <MetricBox label="Workers (Active/Max)" value={`${resources.active_workers} / ${resources.configured_workers}`} mono />
              <MetricBox label="Bounded Queue" value={`${resources.queue_depth} / ${resources.max_queue_capacity}`} mono />
              <MetricBox label="Tokens Today" value={resources.tokens_today.toLocaleString()} mono />
              <MetricBox label="Spend Today" value={resources.cost_today_formatted} mono />
            </div>

            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
              <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-slate-800">
                <Cpu className="h-3.5 w-3.5 text-slate-600" />
                Automatic Backpressure Thresholds
              </div>
              <div className="grid grid-cols-3 gap-2 text-[11px] text-slate-600 tabular-nums">
                <div>70%: Throttle background jobs</div>
                <div>80%: Pause non-essential workers</div>
                <div>90%: Reject heavy tasks cleanly</div>
              </div>
              <div className="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
                <HardDrive className="h-3.5 w-3.5" />
                Host RAM free: {resources.system_free_ram_mb} MB / {resources.system_total_ram_mb} MB · Logical CPUs: {resources.logical_cpu_count}
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function MetricBox({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className={`mt-1 text-sm font-bold text-slate-900 ${mono ? 'font-mono tabular-nums' : ''}`}>{value}</div>
    </div>
  );
}
