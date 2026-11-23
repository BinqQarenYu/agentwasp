import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Database,
  ExternalLink,
  GitBranch,
  RefreshCw,
  User,
  Users,
  X,
} from 'lucide-react';
import type { AgentHealth, AgentStatus, CanonicalAgent, TaskRecord } from '../shared/types';
import { apiRequest } from '../api/client';
import { AgentHealthChip, AgentStatusChip, formatShortTime } from './StatusBadge';

interface GraphNode {
  id: string;
  label: string;
  subtitle: string;
  type: 'supervisor' | 'agent' | 'knowledge';
  status?: AgentStatus;
  health?: AgentHealth;
  owner_id?: string;
  team_name?: string;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: 'reports_to' | 'handoff' | 'knowledge' | 'blocked';
  label?: string;
  source_record_id: string;
}

interface BlockedDependency {
  agent_id: string;
  display_name: string;
  target: string;
  summary: string;
  incident_id: string | null;
}

interface OrgGraphPayload {
  team: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  blocked_dependencies: BlockedDependency[];
  updated_at: string;
}

interface TaskHandoffsPayload {
  handoffs: Array<{
    task_id: string;
    title: string;
    from_agent_id: string;
    from_agent_name: string;
    to_agent_id: string;
    to_agent_name: string;
    label: string;
    status: string;
  }>;
  unowned_tasks: TaskRecord[];
  overloaded_agents: Array<{ agent_id: string; display_name: string; queue_depth: number }>;
  blocked_tasks: TaskRecord[];
  circular_handoffs: string[][];
  data_as_of: string;
}

interface NodeInspectorPayload {
  node_type: 'agent' | 'task' | 'knowledge' | 'supervisor';
  node_id: string;
  record: Record<string, unknown>;
}

interface OrgHandoffViewProps {
  teams: Array<{ id: string; name: string }>;
  agents: CanonicalAgent[];
  selectedAgentId: string | null;
  refreshNonce: number;
  expanded?: boolean;
  onSelectAgent: (agentId: string) => void;
  onMutated: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}

export function OrgHandoffView({
  teams,
  agents,
  selectedAgentId,
  refreshNonce,
  expanded = false,
  onSelectAgent,
  onMutated,
  onNotify,
}: OrgHandoffViewProps) {
  const [mode, setMode] = useState<'reporting' | 'task_flow'>('reporting');
  const [team, setTeam] = useState<string>('all');
  const [graph, setGraph] = useState<OrgGraphPayload | null>(null);
  const [taskFlow, setTaskFlow] = useState<TaskHandoffsPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inspectedNode, setInspectedNode] = useState<NodeInspectorPayload | null>(null);
  const [inspectLoading, setInspectLoading] = useState(false);
  const [assigningTaskId, setAssigningTaskId] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [g, tf] = await Promise.all([
        apiRequest<OrgGraphPayload>(`/organization/graph?team=${encodeURIComponent(team)}`),
        apiRequest<TaskHandoffsPayload>('/tasks/handoffs'),
      ]);
      setGraph(g);
      setTaskFlow(tf);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load organization graph');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [team, refreshNonce]);

  const inspectNode = async (nodeId: string) => {
    setInspectLoading(true);
    try {
      const data = await apiRequest<NodeInspectorPayload>(`/graph/nodes/${encodeURIComponent(nodeId)}`);
      setInspectedNode(data);
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Failed to inspect graph node', 'error');
    } finally {
      setInspectLoading(false);
    }
  };

  const assignUnownedTask = async (taskId: string, agentId: string) => {
    if (!agentId) return;
    setAssigningTaskId(taskId);
    try {
      await apiRequest(`/tasks/${taskId}`, {
        method: 'PATCH',
        body: { agent_id: agentId, reason: 'Assigned from Organization & Handoff view' },
      });
      onNotify(`Assigned ${taskId} to ${agentId}`, 'success');
      onMutated();
      void loadData();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Task assignment failed', 'error');
    } finally {
      setAssigningTaskId(null);
    }
  };

  const supervisors = graph?.nodes.filter((n) => n.type === 'supervisor') ?? [];
  const agentNodes = graph?.nodes.filter((n) => n.type === 'agent') ?? [];
  const primarySupervisor = supervisors.find((s) => s.id === 'usr-jlee') ?? supervisors[0];
  const atlasNode = agentNodes.find((a) => a.id === 'AG-001');
  const scribeNode = agentNodes.find((a) => a.id === 'AG-004');
  const ledgerNode = agentNodes.find((a) => a.id === 'AG-002');
  const extraAgents = agentNodes.filter((a) => !['AG-001', 'AG-004', 'AG-002'].includes(a.id));

  const primaryBlocked = graph?.blocked_dependencies[0] ?? null;

  return (
    <section aria-label="Organization and Handoff View" className="flex flex-col rounded-xl border border-slate-200 bg-white">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex h-6 items-center rounded bg-blue-600 px-2 font-mono text-xs font-semibold text-white">
            03
          </span>
          <h2 className="font-display text-base font-bold text-slate-900">Organization &amp; Handoff View</h2>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <span>Team:</span>
            <select
              aria-label="Filter organization team"
              value={team}
              onChange={(e) => setTeam(e.target.value)}
              className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-800 focus:border-blue-500 focus:outline-none"
            >
              <option value="all">Operations (All)</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>

          <div role="tablist" aria-label="Graph view mode" className="inline-flex rounded-md border border-slate-200 bg-slate-50 p-0.5">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'reporting'}
              onClick={() => setMode('reporting')}
              className={`rounded px-2.5 py-1 text-xs font-semibold transition ${
                mode === 'reporting' ? 'bg-blue-600 text-white' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              Reporting
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'task_flow'}
              onClick={() => setMode('task_flow')}
              className={`rounded px-2.5 py-1 text-xs font-semibold transition ${
                mode === 'task_flow' ? 'bg-blue-600 text-white' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              Task flow
            </button>
          </div>

          <button
            type="button"
            onClick={() => void loadData()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-5 mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 p-4">
        {mode === 'reporting' ? (
          <div className={`grid gap-4 ${expanded ? 'lg:grid-cols-12' : 'md:grid-cols-12'}`}>
            {/* Interactive Graph Canvas */}
            <div className={`${expanded ? 'lg:col-span-8' : 'md:col-span-8'} rounded-lg border border-slate-200 bg-slate-50/70 p-4`}>
              {atlasNode && scribeNode && ledgerNode && primarySupervisor ? (
                <div className="relative mx-auto max-w-[440px] select-none">
                  {/* SVG Connector Overlay */}
                  <svg
                    viewBox="0 0 420 265"
                    className="h-auto w-full"
                    role="img"
                    aria-label="Reporting hierarchy and task handoff topology"
                  >
                    <defs>
                      <marker id="arrow-solid" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                        <path d="M 0 1 L 10 5 L 0 9 z" fill="#334155" />
                      </marker>
                      <marker id="arrow-dashed" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                        <path d="M 0 1 L 10 5 L 0 9 z" fill="#2563EB" />
                      </marker>
                    </defs>

                    {/* Solid: Supervisor -> Atlas & Scribe */}
                    <path d="M 210 52 L 210 68 L 100 68 L 100 94" fill="none" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-solid)" />
                    <path d="M 210 68 L 320 68 L 320 94" fill="none" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-solid)" />

                    {/* Dashed: Atlas -> Ledger (Evidence handoff) */}
                    <path d="M 100 144 L 100 198" fill="none" stroke="#2563EB" strokeWidth="1.6" strokeDasharray="4 3" markerEnd="url(#arrow-dashed)" />

                    {/* Solid: Atlas -> Shared Knowledge */}
                    <path d="M 152 140 L 266 208" fill="none" stroke="#334155" strokeWidth="1.4" markerEnd="url(#arrow-solid)" />

                    {/* Solid: Scribe -> Shared Knowledge */}
                    <path d="M 320 144 L 320 188" fill="none" stroke="#334155" strokeWidth="1.4" markerEnd="url(#arrow-solid)" />

                    {/* Supervisor Node */}
                    <g
                      className="cursor-pointer"
                      onClick={() => void inspectNode(primarySupervisor.id)}
                    >
                      <rect x="132" y="8" width="156" height="44" rx="8" fill="#EFF6FF" stroke="#3B82F6" strokeWidth="1.5" />
                      <text x="210" y="27" textAnchor="middle" className="fill-slate-900 text-[12px] font-bold">
                        {primarySupervisor.label}
                      </text>
                      <text x="210" y="42" textAnchor="middle" className="fill-slate-600 text-[10px]">
                        Human supervisor
                      </text>
                    </g>

                    {/* Atlas Node */}
                    <g
                      className="cursor-pointer"
                      onClick={() => {
                        onSelectAgent(atlasNode.id);
                        void inspectNode(atlasNode.id);
                      }}
                    >
                      <rect
                        x="32"
                        y="98"
                        width="136"
                        height="46"
                        rx="8"
                        fill={selectedAgentId === atlasNode.id ? '#EFF6FF' : '#FFFFFF'}
                        stroke={selectedAgentId === atlasNode.id ? '#2563EB' : '#94A3B8'}
                        strokeWidth={selectedAgentId === atlasNode.id ? '2' : '1.4'}
                      />
                      <text x="100" y="118" textAnchor="middle" className="fill-slate-900 text-[12px] font-bold">
                        {atlasNode.label}
                      </text>
                      <text x="100" y="133" textAnchor="middle" className="fill-slate-500 text-[10px]">
                        {atlasNode.subtitle}
                      </text>
                    </g>

                    {/* Scribe Node */}
                    <g
                      className="cursor-pointer"
                      onClick={() => {
                        onSelectAgent(scribeNode.id);
                        void inspectNode(scribeNode.id);
                      }}
                    >
                      <rect
                        x="252"
                        y="98"
                        width="136"
                        height="46"
                        rx="8"
                        fill={selectedAgentId === scribeNode.id ? '#EFF6FF' : '#FFFFFF'}
                        stroke={selectedAgentId === scribeNode.id ? '#2563EB' : '#94A3B8'}
                        strokeWidth={selectedAgentId === scribeNode.id ? '2' : '1.4'}
                      />
                      <text x="320" y="118" textAnchor="middle" className="fill-slate-900 text-[12px] font-bold">
                        {scribeNode.label}
                      </text>
                      <text x="320" y="133" textAnchor="middle" className="fill-slate-500 text-[10px]">
                        {scribeNode.subtitle}
                      </text>
                    </g>

                    {/* Evidence handoff pill */}
                    <g className="cursor-pointer" onClick={() => setMode('task_flow')}>
                      <rect x="44" y="160" width="112" height="22" rx="4" fill="#DBEAFE" stroke="#93C5FD" strokeWidth="1" />
                      <text x="100" y="174" textAnchor="middle" className="fill-blue-800 text-[10px] font-semibold">
                        Evidence handoff
                      </text>
                    </g>

                    {/* Ledger Node */}
                    <g
                      className="cursor-pointer"
                      onClick={() => {
                        onSelectAgent(ledgerNode.id);
                        void inspectNode(ledgerNode.id);
                      }}
                    >
                      <rect
                        x="32"
                        y="202"
                        width="136"
                        height="46"
                        rx="8"
                        fill={selectedAgentId === ledgerNode.id ? '#EFF6FF' : '#FFFFFF'}
                        stroke={selectedAgentId === ledgerNode.id ? '#2563EB' : '#94A3B8'}
                        strokeWidth={selectedAgentId === ledgerNode.id ? '2' : '1.4'}
                      />
                      <text x="100" y="222" textAnchor="middle" className="fill-slate-900 text-[12px] font-bold">
                        {ledgerNode.label}
                      </text>
                      <text x="100" y="237" textAnchor="middle" className="fill-slate-500 text-[10px]">
                        {ledgerNode.subtitle}
                      </text>
                    </g>

                    {/* Shared Knowledge Cylinder */}
                    <g className="cursor-pointer" onClick={() => void inspectNode('SK-01')}>
                      <path
                        d="M 268 202 C 268 192, 372 192, 372 202 L 372 240 C 372 250, 268 250, 268 240 Z"
                        fill="#EFF6FF"
                        stroke="#3B82F6"
                        strokeWidth="1.4"
                      />
                      <ellipse cx="320" cy="202" rx="52" ry="9" fill="#DBEAFE" stroke="#3B82F6" strokeWidth="1.4" />
                      <text x="320" y="225" textAnchor="middle" className="fill-slate-900 text-[11px] font-semibold">
                        Shared
                      </text>
                      <text x="320" y="238" textAnchor="middle" className="fill-slate-700 text-[11px]">
                        knowledge
                      </text>
                    </g>
                  </svg>
                </div>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs font-semibold text-slate-700">Team Topology ({agentNodes.length} agents)</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {agentNodes.map((n) => (
                      <button
                        key={n.id}
                        type="button"
                        onClick={() => {
                          onSelectAgent(n.id);
                          void inspectNode(n.id);
                        }}
                        className={`flex items-center justify-between rounded-lg border px-3 py-2 text-left text-xs transition ${
                          selectedAgentId === n.id
                            ? 'border-blue-500 bg-blue-50'
                            : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                      >
                        <div>
                          <div className="font-semibold text-slate-900">{n.label}</div>
                          <div className="text-[11px] text-slate-500">{n.subtitle}</div>
                        </div>
                        {n.status && <AgentStatusChip status={n.status} />}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Additional team agents quick-inspect row */}
              {extraAgents.length > 0 && atlasNode && (
                <div className="mt-3 border-t border-slate-200 pt-2.5">
                  <div className="mb-1.5 flex items-center justify-between text-[11px] font-medium text-slate-500">
                    <span>Additional nodes in scope ({extraAgents.length})</span>
                    <span>Click any node to inspect</span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {extraAgents.slice(0, expanded ? 18 : 6).map((a) => (
                      <button
                        key={a.id}
                        type="button"
                        onClick={() => {
                          onSelectAgent(a.id);
                          void inspectNode(a.id);
                        }}
                        className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition ${
                          selectedAgentId === a.id
                            ? 'border-blue-500 bg-blue-50 text-blue-800'
                            : a.status === 'blocked'
                              ? 'border-red-200 bg-red-50 text-red-700 hover:bg-red-100'
                              : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-100'
                        }`}
                      >
                        <Bot className="h-3 w-3" />
                        {a.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Right Sidebar inside Card: Blocked Alert + Legend + Node Inspector */}
            <div className={`${expanded ? 'lg:col-span-4' : 'md:col-span-4'} flex flex-col gap-3`}>
              {/* Blocked dependency alert */}
              {primaryBlocked ? (
                <button
                  type="button"
                  onClick={() => {
                    onSelectAgent(primaryBlocked.agent_id);
                    void inspectNode(primaryBlocked.agent_id);
                  }}
                  className="w-full rounded-lg border border-red-200 bg-red-50/90 p-3 text-left transition hover:bg-red-100/70"
                >
                  <div className="flex items-start gap-2">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                    <div>
                      <div className="text-xs font-bold text-red-900">Blocked dependency:</div>
                      <div className="mt-0.5 text-xs font-medium text-red-800">
                        {primaryBlocked.display_name} → {primaryBlocked.target}
                      </div>
                      <p className="mt-1 text-[11px] leading-snug text-red-700">{primaryBlocked.summary}</p>
                    </div>
                  </div>
                </button>
              ) : (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">
                  No blocked dependencies in active team scope.
                </div>
              )}

              {/* Legend */}
              <div className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="text-xs font-bold text-slate-800">Legend</div>
                <div className="mt-2 space-y-2 text-xs text-slate-600">
                  <div className="flex items-center gap-2">
                    <span className="inline-block h-0.5 w-7 bg-slate-700" />
                    <span>Solid: reports to</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="inline-block w-7 border-t-2 border-dashed border-blue-600" />
                    <span>Dashed: task handoff</span>
                  </div>
                </div>
              </div>

              {/* Node Inspector Drawer Card */}
              {inspectLoading ? (
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">
                  Loading canonical node record…
                </div>
              ) : inspectedNode ? (
                <div className="rounded-lg border border-blue-200 bg-blue-50/40 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] font-semibold uppercase tracking-wide text-blue-700">
                      {inspectedNode.node_type} · {inspectedNode.node_id}
                    </span>
                    <button
                      type="button"
                      aria-label="Close inspector"
                      onClick={() => setInspectedNode(null)}
                      className="rounded p-0.5 text-slate-400 hover:bg-white hover:text-slate-700"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>

                  {inspectedNode.node_type === 'agent' && (
                    <div className="mt-2 space-y-1.5 text-xs">
                      <div className="font-bold text-slate-900">
                        {String(inspectedNode.record.display_name)} — {String(inspectedNode.record.role)}
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <AgentStatusChip status={inspectedNode.record.status as AgentStatus} />
                        <AgentHealthChip health={inspectedNode.record.health as AgentHealth} />
                      </div>
                      <div className="text-[11px] text-slate-600">
                        Task: <span className="font-medium text-slate-800">{String(inspectedNode.record.current_task_title || '—')}</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => onSelectAgent(inspectedNode.node_id)}
                        className="mt-1 inline-flex items-center gap-1 text-xs font-semibold text-blue-700 hover:underline"
                      >
                        Focus in Agent Workspace <ExternalLink className="h-3 w-3" />
                      </button>
                    </div>
                  )}

                  {inspectedNode.node_type === 'supervisor' && (
                    <div className="mt-2 space-y-1.5 text-xs">
                      <div className="flex items-center gap-1.5 font-bold text-slate-900">
                        <User className="h-3.5 w-3.5 text-blue-600" />
                        {String(inspectedNode.record.owner_name)}
                      </div>
                      <div className="text-[11px] text-slate-600">
                        Direct reports:{' '}
                        {((inspectedNode.record.direct_reports as Array<{ agent_id: string; display_name: string }>) ?? []).length}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {((inspectedNode.record.direct_reports as Array<{ agent_id: string; display_name: string }>) ?? []).map((r) => (
                          <button
                            key={r.agent_id}
                            type="button"
                            onClick={() => onSelectAgent(r.agent_id)}
                            className="rounded border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:border-blue-400"
                          >
                            {r.display_name}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {inspectedNode.node_type === 'knowledge' && (
                    <div className="mt-2 space-y-1 text-xs">
                      <div className="flex items-center gap-1.5 font-bold text-slate-900">
                        <Database className="h-3.5 w-3.5 text-blue-600" />
                        Shared Knowledge Graph
                      </div>
                      <div className="text-[11px] text-slate-600 tabular-nums">
                        Visible provenance memories: {String(inspectedNode.record.memory_items_count ?? 0)}
                      </div>
                      <div className="text-[11px] text-slate-600 tabular-nums">
                        Graphify v{String((inspectedNode.record.graphify as { graph_version?: number })?.graph_version ?? 1)} ·{' '}
                        {String((inspectedNode.record.graphify as { node_count?: number })?.node_count ?? 0)} nodes
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-200 p-3 text-[11px] text-slate-500">
                  Select any supervisor, agent, or knowledge node to inspect its permission-scoped canonical record.
                </div>
              )}
            </div>
          </div>
        ) : (
          /* Task Flow & Handoff Diagnostics View */
          <div className="space-y-3">
            {/* Active Handoffs */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-slate-700">
                  <GitBranch className="h-3.5 w-3.5 text-blue-600" />
                  Active Task Handoffs ({taskFlow?.handoffs.length ?? 0})
                </h3>
                {taskFlow && taskFlow.circular_handoffs.length > 0 && (
                  <span className="rounded bg-red-100 px-2 py-0.5 text-[11px] font-semibold text-red-700">
                    {taskFlow.circular_handoffs.length} cycle detected
                  </span>
                )}
              </div>
              <div className="space-y-1.5">
                {(taskFlow?.handoffs ?? []).map((h) => (
                  <div
                    key={h.task_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[11px] text-slate-500">{h.task_id}</span>
                      <button
                        type="button"
                        onClick={() => onSelectAgent(h.from_agent_id)}
                        className="font-semibold text-slate-900 hover:text-blue-600"
                      >
                        {h.from_agent_name}
                      </button>
                      <ArrowRight className="h-3.5 w-3.5 text-blue-600" />
                      <button
                        type="button"
                        onClick={() => onSelectAgent(h.to_agent_id)}
                        className="font-semibold text-slate-900 hover:text-blue-600"
                      >
                        {h.to_agent_name}
                      </button>
                    </div>
                    <span className="rounded bg-blue-100 px-2 py-0.5 text-[11px] font-medium text-blue-800">
                      {h.label}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* Unowned Tasks + Overloaded Agents */}
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
                <div className="mb-1.5 text-xs font-bold text-amber-900">
                  Unowned Tasks ({taskFlow?.unowned_tasks.length ?? 0})
                </div>
                {taskFlow && taskFlow.unowned_tasks.length > 0 ? (
                  <div className="space-y-2">
                    {taskFlow.unowned_tasks.map((t) => (
                      <div key={t.task_id} className="rounded border border-amber-200 bg-white p-2 text-xs">
                        <div className="font-semibold text-slate-900">
                          {t.task_id} · {t.title}
                        </div>
                        <div className="mt-1.5 flex items-center gap-1.5">
                          <select
                            aria-label={`Assign ${t.task_id}`}
                            disabled={assigningTaskId === t.task_id}
                            defaultValue=""
                            onChange={(e) => void assignUnownedTask(t.task_id, e.target.value)}
                            className="w-full rounded border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-700"
                          >
                            <option value="">Assign to agent…</option>
                            {agents.map((a) => (
                              <option key={a.agent_id} value={a.agent_id}>
                                {a.display_name} ({a.role})
                              </option>
                            ))}
                          </select>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-[11px] text-amber-800">All active tasks have a canonical owner.</p>
                )}
              </div>

              <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
                <div className="mb-1.5 flex items-center gap-1.5 text-xs font-bold text-slate-800">
                  <Users className="h-3.5 w-3.5 text-slate-600" />
                  Overloaded Agents ({taskFlow?.overloaded_agents.length ?? 0})
                </div>
                {taskFlow && taskFlow.overloaded_agents.length > 0 ? (
                  <div className="space-y-1.5">
                    {taskFlow.overloaded_agents.map((oa) => (
                      <button
                        key={oa.agent_id}
                        type="button"
                        onClick={() => onSelectAgent(oa.agent_id)}
                        className="flex w-full items-center justify-between rounded border border-slate-200 bg-white px-2.5 py-1.5 text-xs hover:border-blue-400"
                      >
                        <span className="font-semibold text-slate-900">{oa.display_name}</span>
                        <span className="font-mono text-[11px] font-semibold text-amber-700 tabular-nums">
                          Queue: {oa.queue_depth}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-[11px] text-slate-600">No overloaded queues detected.</p>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t border-slate-200 bg-slate-50/60 px-5 py-2.5 text-[11px] text-slate-500">
        <span>Permission-scoped graph · Updated {formatShortTime(graph?.updated_at ?? null)}</span>
        <span className="rounded border border-slate-200 bg-white px-2 py-0.5 font-medium text-slate-600">
          Live topology
        </span>
      </div>
    </section>
  );
}
