import React, { useMemo, useState } from 'react';
import {
  Activity,
  AlertOctagon,
  ArrowUpDown,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Columns3,
  Coins,
  Download,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  UserPlus,
  Users,
  X,
} from 'lucide-react';
import type { CanonicalAgent, WorkforceSummary } from '../shared/types';
import { apiRequest, getClientAuth, makeIdempotencyKey } from '../api/client';
import { AgentHealthChip, AgentStatusChip, formatShortTime, RiskText } from './StatusBadge';

export interface WorkforceFilters {
  query: string;
  status: string;
  team_id: string;
  owner_id: string;
  provider: string;
  kpi_filter: string;
  sort: string;
  sort_dir: 'asc' | 'desc';
  page: number;
  page_size: number;
}

interface CommandCenterProps {
  summary: WorkforceSummary | null;
  agents: Array<CanonicalAgent & { cost_today_formatted?: string }>;
  totalAgents: number;
  totalPages: number;
  facets: {
    teams: Array<{ id: string; name: string }>;
    owners: Array<{ id: string; name: string }>;
    providers: string[];
  };
  filters: WorkforceFilters;
  loading: boolean;
  error: string | null;
  selectedAgentId: string | null;
  onFiltersChange: (patch: Partial<WorkforceFilters>) => void;
  onSelectAgent: (agentId: string) => void;
  onRefresh: () => void;
  onNotify: (msg: string, tone?: 'info' | 'success' | 'error') => void;
}

type OptionalCol = 'team' | 'risk' | 'latency' | 'sla';

const SAVED_VIEWS: Array<{ id: string; label: string; filter: Partial<WorkforceFilters> }> = [
  { id: 'all', label: 'All Agents', filter: { kpi_filter: 'all', status: 'all', health: undefined } as Partial<WorkforceFilters> },
  { id: 'active', label: 'Active', filter: { kpi_filter: 'active', status: 'all' } },
  { id: 'blocked', label: 'Blocked', filter: { kpi_filter: 'blocked', status: 'all' } },
  { id: 'approval', label: 'Awaiting Approval', filter: { kpi_filter: 'approval', status: 'all' } },
  { id: 'high_cost', label: 'High Cost', filter: { kpi_filter: 'high_cost', status: 'all' } },
  { id: 'unhealthy', label: 'Unhealthy', filter: { kpi_filter: 'unhealthy', status: 'all' } },
  { id: 'available', label: 'Idle / Available', filter: { kpi_filter: 'available', status: 'all' } },
];

export function CommandCenter({
  summary,
  agents,
  totalAgents,
  totalPages,
  facets,
  filters,
  loading,
  error,
  selectedAgentId,
  onFiltersChange,
  onSelectAgent,
  onRefresh,
  onNotify,
}: CommandCenterProps) {
  const [checkedIds, setCheckedIds] = useState<string[]>([]);
  const [showColumnsMenu, setShowColumnsMenu] = useState(false);
  const [extraCols, setExtraCols] = useState<OptionalCol[]>([]);
  const [openRowMenu, setOpenRowMenu] = useState<string | null>(null);
  const [showRegisterModal, setShowRegisterModal] = useState(false);
  const [showBulkAssignModal, setShowBulkAssignModal] = useState(false);
  const [bulkTaskTitle, setBulkTaskTitle] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);

  // Register form state
  const [regName, setRegName] = useState('');
  const [regRole, setRegRole] = useState('');
  const [regTeam, setRegTeam] = useState('Operations');
  const [regOwner, setRegOwner] = useState('J. Lee');
  const [regProvider, setRegProvider] = useState('Google');
  const [regModel, setRegModel] = useState('Gemini 2.5 Flash');
  const [regRisk, setRegRisk] = useState<'low' | 'medium' | 'high'>('low');
  const [regSubmitting, setRegSubmitting] = useState(false);

  const visibleIds = useMemo(() => agents.map((a) => a.agent_id), [agents]);
  const allVisibleChecked = visibleIds.length > 0 && visibleIds.every((id) => checkedIds.includes(id));

  const toggleAllVisible = () => {
    if (allVisibleChecked) {
      setCheckedIds((prev) => prev.filter((id) => !visibleIds.includes(id)));
    } else {
      setCheckedIds((prev) => Array.from(new Set([...prev, ...visibleIds])));
    }
  };

  const toggleRowCheck = (id: string) => {
    setCheckedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const toggleSort = (col: string) => {
    if (filters.sort === col) {
      onFiltersChange({ sort_dir: filters.sort_dir === 'asc' ? 'desc' : 'asc', page: 1 });
    } else {
      onFiltersChange({ sort: col, sort_dir: 'asc', page: 1 });
    }
  };

  const toggleKpiFilter = (kpi: string) => {
    onFiltersChange({
      kpi_filter: filters.kpi_filter === kpi ? 'all' : kpi,
      status: 'all',
      page: 1,
    });
  };

  const toggleExtraCol = (col: OptionalCol) => {
    setExtraCols((prev) => (prev.includes(col) ? prev.filter((c) => c !== col) : [...prev, col]));
  };

  const runRowQuickCommand = async (agent: CanonicalAgent, command: 'pause' | 'resume' | 'safe_stop' | 'request_approval') => {
    setOpenRowMenu(null);
    try {
      const res = await apiRequest<{ current_status: string; audit_id: string; correlation_id: string }>(
        `/agents/${agent.agent_id}/commands`,
        {
          method: 'POST',
          body: {
            command,
            idempotency_key: makeIdempotencyKey(`row_${command}`),
            expected_version: agent.version,
            reason: `Command Center quick action: ${command}`,
          },
        },
      );
      onNotify(`${agent.display_name} → ${res.current_status} (Audit ${res.audit_id})`, 'success');
      onRefresh();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Command failed', 'error');
    }
  };

  const runBulkAction = async (action: 'pause' | 'request_approval' | 'assign_task', taskTitle?: string) => {
    if (checkedIds.length === 0) return;
    setBulkBusy(true);
    try {
      const res = await apiRequest<{
        results: Array<{ agent_id: string; ok: boolean; error?: string }>;
        correlation_id: string;
      }>('/agents/bulk-actions', {
        method: 'POST',
        body: {
          agent_ids: checkedIds,
          action,
          preview: false,
          task_title: taskTitle,
          reason: `Bulk ${action} on ${checkedIds.length} agent(s)`,
        },
      });
      const okCount = res.results.filter((r) => r.ok).length;
      const failCount = res.results.length - okCount;
      onNotify(
        `Bulk ${action}: ${okCount} succeeded${failCount > 0 ? `, ${failCount} skipped` : ''} (${res.correlation_id})`,
        failCount > 0 ? 'info' : 'success',
      );
      setShowBulkAssignModal(false);
      setBulkTaskTitle('');
      onRefresh();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Bulk action failed', 'error');
    } finally {
      setBulkBusy(false);
    }
  };

  const handleRegisterAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!regName.trim() || !regRole.trim()) return;
    setRegSubmitting(true);
    try {
      const res = await apiRequest<{ agent: CanonicalAgent; audit_id: string }>('/agents', {
        method: 'POST',
        body: {
          display_name: regName.trim(),
          role: regRole.trim(),
          team_name: regTeam,
          owner_name: regOwner,
          provider: regProvider,
          model: regModel,
          risk_level: regRisk,
        },
      });
      onNotify(`Registered ${res.agent.display_name} (${res.agent.agent_id}) · Audit ${res.audit_id}`, 'success');
      setShowRegisterModal(false);
      setRegName('');
      setRegRole('');
      onSelectAgent(res.agent.agent_id);
      onRefresh();
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Registration failed', 'error');
    } finally {
      setRegSubmitting(false);
    }
  };

  const handleExport = async (format: 'csv' | 'json') => {
    const qs = new URLSearchParams();
    qs.set('format', format);
    if (filters.query) qs.set('query', filters.query);
    if (filters.status !== 'all') qs.set('status', filters.status);
    if (filters.team_id !== 'all') qs.set('team_id', filters.team_id);
    if (filters.owner_id !== 'all') qs.set('owner_id', filters.owner_id);
    if (filters.provider !== 'all') qs.set('provider', filters.provider);
    if (filters.kpi_filter !== 'all') qs.set('kpi_filter', filters.kpi_filter);

    try {
      const auth = getClientAuth();
      const res = await fetch(`/api/v1/agents/export?${qs.toString()}`, {
        headers: {
          'X-Tenant-Id': auth.tenantId,
          'X-Actor-Id': auth.actorId,
          'X-Actor-Name': auth.actorName,
          'X-Actor-Role': auth.actorRole,
        },
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.error?.message || `Export failed (${res.status})`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = format === 'csv' ? 'agent-workforce.csv' : 'agent-workforce.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      onNotify(`Exported workforce snapshot (${format.toUpperCase()})`, 'success');
    } catch (err) {
      onNotify(err instanceof Error ? err.message : 'Export failed', 'error');
    }
  };

  const startIdx = totalAgents === 0 ? 0 : (filters.page - 1) * filters.page_size + 1;
  const endIdx = Math.min(totalAgents, filters.page * filters.page_size);

  return (
    <section aria-label="Workforce Command Center" className="rounded-xl border border-slate-200 bg-white">
      {/* Header Row */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="inline-flex h-6 items-center rounded bg-blue-600 px-2 font-mono text-xs font-semibold text-white">
            01
          </span>
          <div>
            <h2 className="text-lg font-bold tracking-tight text-slate-900">Workforce Command Center</h2>
            <p className="text-xs text-slate-500">
              {summary ? `${summary.total_workforce} canonical agents · SLA ${summary.sla_compliance_pct}%` : 'Loading workforce scope...'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <span className="text-xs text-slate-500 tabular-nums">
            Last updated {formatShortTime(summary?.data_as_of)}
          </span>
          <button
            type="button"
            onClick={onRefresh}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-blue-600 whitespace-nowrap"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setShowRegisterModal(true)}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 whitespace-nowrap"
          >
            <Plus className="h-3.5 w-3.5" />
            Register agent
          </button>
        </div>
      </div>

      {/* 6 Reconciled Clickable KPI Cards */}
      <div className="grid grid-cols-2 gap-3 px-5 pt-4 md:grid-cols-3 xl:grid-cols-6">
        <button
          type="button"
          onClick={() => toggleKpiFilter('active')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'active'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
            <Users className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Active</div>
            <div className="font-mono text-xl font-bold text-slate-900 tabular-nums">
              {summary ? summary.active : '—'}
            </div>
          </div>
        </button>

        <button
          type="button"
          onClick={() => toggleKpiFilter('available')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'available'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-600">
            <CheckCircle2 className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Available</div>
            <div className="font-mono text-xl font-bold text-slate-900 tabular-nums">
              {summary ? summary.available : '—'}
            </div>
          </div>
        </button>

        <button
          type="button"
          onClick={() => toggleKpiFilter('blocked')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'blocked'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-rose-50 text-rose-600">
            <AlertOctagon className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Blocked</div>
            <div className="font-mono text-xl font-bold text-slate-900 tabular-nums">
              {summary ? summary.blocked : '—'}
            </div>
          </div>
        </button>

        <button
          type="button"
          onClick={() => toggleKpiFilter('approval')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'approval'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-violet-50 text-violet-600">
            <Clock className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Approval</div>
            <div className="font-mono text-xl font-bold text-slate-900 tabular-nums">
              {summary ? summary.awaiting_approval : '—'}
            </div>
          </div>
        </button>

        <button
          type="button"
          onClick={() => toggleKpiFilter('high_cost')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'high_cost'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
            <Coins className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Spend today</div>
            <div className="font-mono text-xl font-bold text-slate-900 tabular-nums">
              {summary ? summary.spend_today_formatted : '—'}
            </div>
          </div>
        </button>

        <button
          type="button"
          onClick={() => toggleKpiFilter('pressured')}
          className={`flex items-center gap-3 rounded-lg border p-3 text-left transition-colors ${
            filters.kpi_filter === 'pressured'
              ? 'border-blue-500 bg-blue-50/50 ring-1 ring-blue-500'
              : 'border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60'
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-700">
            <Activity className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-xs text-slate-500">Resource pressure</div>
            <div className="mt-0.5 text-sm font-semibold text-emerald-700">
              {summary ? summary.resource_pressure : '—'}
            </div>
          </div>
        </button>
      </div>

      {/* Saved Views Strip */}
      <div className="flex flex-wrap items-center gap-1.5 px-5 pt-3">
        {SAVED_VIEWS.map((view) => {
          const active = filters.kpi_filter === view.id || (view.id === 'all' && filters.kpi_filter === 'all' && filters.status === 'all');
          return (
            <button
              key={view.id}
              type="button"
              onClick={() => onFiltersChange({ ...view.filter, page: 1 })}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors whitespace-nowrap ${
                active
                  ? 'bg-slate-900 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200 hover:text-slate-900'
              }`}
            >
              {view.label}
            </button>
          );
        })}
      </div>

      {/* Filter Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2.5 px-5 py-3">
        <div className="flex flex-wrap items-center gap-2 flex-1 min-w-[260px]">
          <div className="relative flex-1 min-w-[200px] max-w-xs">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              aria-label="Search agents"
              value={filters.query}
              onChange={(e) => onFiltersChange({ query: e.target.value, page: 1 })}
              placeholder="Search agents, IDs, roles, models..."
              className="w-full rounded-lg border border-slate-200 bg-white py-1.5 pl-8 pr-3 text-xs text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          <select
            aria-label="Filter by status"
            value={filters.status}
            onChange={(e) => onFiltersChange({ status: e.target.value, kpi_filter: 'all', page: 1 })}
            className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-500 focus:outline-none"
          >
            <option value="all">Status: All</option>
            <option value="running">Working</option>
            <option value="idle">Available</option>
            <option value="blocked">Blocked</option>
            <option value="awaiting_approval">Awaiting approval</option>
            <option value="paused">Paused</option>
            <option value="degraded">Degraded</option>
            <option value="disabled">Disabled</option>
          </select>

          <select
            aria-label="Filter by team"
            value={filters.team_id}
            onChange={(e) => onFiltersChange({ team_id: e.target.value, page: 1 })}
            className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-500 focus:outline-none"
          >
            <option value="all">Team: All</option>
            {facets.teams.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>

          <select
            aria-label="Filter by owner"
            value={filters.owner_id}
            onChange={(e) => onFiltersChange({ owner_id: e.target.value, page: 1 })}
            className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-500 focus:outline-none"
          >
            <option value="all">Owner: All</option>
            {facets.owners.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>

          <select
            aria-label="Filter by provider"
            value={filters.provider}
            onChange={(e) => onFiltersChange({ provider: e.target.value, page: 1 })}
            className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-500 focus:outline-none"
          >
            <option value="all">Provider: All</option>
            {facets.providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>

          {(filters.query || filters.status !== 'all' || filters.team_id !== 'all' || filters.owner_id !== 'all' || filters.provider !== 'all' || filters.kpi_filter !== 'all') && (
            <button
              type="button"
              onClick={() =>
                onFiltersChange({
                  query: '',
                  status: 'all',
                  team_id: 'all',
                  owner_id: 'all',
                  provider: 'all',
                  kpi_filter: 'all',
                  page: 1,
                })
              }
              className="text-xs font-medium text-blue-600 hover:underline whitespace-nowrap"
            >
              Reset filters
            </button>
          )}
        </div>

        <div className="flex items-center gap-2 relative">
          <button
            type="button"
            onClick={() => handleExport('csv')}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 whitespace-nowrap"
            title="Export filtered rows as CSV"
          >
            <Download className="h-3.5 w-3.5" />
            CSV
          </button>
          <button
            type="button"
            onClick={() => handleExport('json')}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 whitespace-nowrap"
            title="Export filtered rows as JSON"
          >
            JSON
          </button>
          <button
            type="button"
            onClick={() => setShowColumnsMenu((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 whitespace-nowrap"
          >
            <Columns3 className="h-3.5 w-3.5" />
            Columns
          </button>

          {showColumnsMenu && (
            <div className="absolute right-0 top-9 z-30 w-44 rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
              <div className="px-2 py-1 text-[11px] font-semibold text-slate-500">Additional columns</div>
              {(
                [
                  ['team', 'Team'],
                  ['risk', 'Risk level'],
                  ['latency', 'Avg latency'],
                  ['sla', 'SLA %'],
                ] as Array<[OptionalCol, string]>
              ).map(([key, label]) => (
                <label
                  key={key}
                  className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
                >
                  <input
                    type="checkbox"
                    checked={extraCols.includes(key)}
                    onChange={() => toggleExtraCol(key)}
                    className="rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  {label}
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="mx-5 mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3.5 py-2.5 text-xs text-rose-800">
          {error}
        </div>
      )}

      {/* High-Density Data Grid */}
      <div className="overflow-x-auto border-t border-slate-200">
        <table className="w-full border-collapse text-left text-xs">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50/80 text-slate-600">
              <th className="w-10 px-4 py-2.5">
                <input
                  type="checkbox"
                  aria-label="Select all visible agents"
                  checked={allVisibleChecked}
                  onChange={toggleAllVisible}
                  className="h-3.5 w-3.5 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
              </th>
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('display_name')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Agent / Role <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('status')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Status <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('owner_name')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Supervisor <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              {extraCols.includes('team') && <th className="px-3 py-2.5 font-semibold">Team</th>}
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('current_task_title')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Current task <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('provider')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Model / Provider <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 text-right font-semibold">
                <button type="button" onClick={() => toggleSort('queue_depth')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Queue <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 text-right font-semibold">
                <button type="button" onClick={() => toggleSort('success_rate_pct')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Success <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="px-3 py-2.5 text-right font-semibold">
                <button type="button" onClick={() => toggleSort('cost_today_cents')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Cost today <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              {extraCols.includes('risk') && <th className="px-3 py-2.5 font-semibold">Risk</th>}
              {extraCols.includes('latency') && <th className="px-3 py-2.5 text-right font-semibold">Latency</th>}
              {extraCols.includes('sla') && <th className="px-3 py-2.5 text-right font-semibold">SLA</th>}
              <th className="px-3 py-2.5 font-semibold">
                <button type="button" onClick={() => toggleSort('health')} className="inline-flex items-center gap-1 hover:text-slate-900">
                  Health <ArrowUpDown className="h-3 w-3 text-slate-400" />
                </button>
              </th>
              <th className="w-10 px-3 py-2.5 text-right font-semibold">Actions</th>
            </tr>
          </thead>

          <tbody className="divide-y divide-slate-100">
            {loading && agents.length === 0 ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={i} className="animate-pulse">
                  <td colSpan={12} className="px-4 py-3">
                    <div className="h-4 w-full rounded bg-slate-100" />
                  </td>
                </tr>
              ))
            ) : agents.length === 0 ? (
              <tr>
                <td colSpan={12} className="px-5 py-10 text-center text-slate-500">
                  <div className="font-medium text-slate-700">No agents match the active filter criteria</div>
                  <p className="mt-1 text-xs">Clear filters or register a new canonical agent into this workspace.</p>
                </td>
              </tr>
            ) : (
              agents.map((agent) => {
                const isSelected = agent.agent_id === selectedAgentId;
                const isChecked = checkedIds.includes(agent.agent_id);
                return (
                  <tr
                    key={agent.agent_id}
                    onClick={() => onSelectAgent(agent.agent_id)}
                    className={`cursor-pointer transition-colors ${
                      isSelected ? 'bg-blue-50/70' : 'hover:bg-slate-50'
                    }`}
                  >
                    <td className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Select ${agent.display_name}`}
                        checked={isChecked}
                        onChange={() => toggleRowCheck(agent.agent_id)}
                        className="h-3.5 w-3.5 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                      />
                    </td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <div className="flex items-baseline gap-1.5">
                        <span className="font-semibold text-slate-900">{agent.display_name}</span>
                        <span className="text-slate-400">/</span>
                        <span className="text-slate-600">{agent.role}</span>
                      </div>
                      <div className="font-mono text-[11px] text-slate-400">{agent.agent_id} · v{agent.version}</div>
                    </td>
                    <td className="px-3 py-2.5">
                      <AgentStatusChip status={agent.status} />
                    </td>
                    <td className="px-3 py-2.5 text-slate-700 whitespace-nowrap">{agent.owner_name}</td>
                    {extraCols.includes('team') && (
                      <td className="px-3 py-2.5 text-slate-600 whitespace-nowrap">{agent.team_name}</td>
                    )}
                    <td className="px-3 py-2.5 max-w-[200px] truncate text-slate-800">
                      {agent.current_task_title || <span className="text-slate-400">—</span>}
                    </td>
                    <td className="px-3 py-2.5 whitespace-nowrap text-slate-700">
                      <span>{agent.model}</span>
                      <span className="mx-1 text-slate-300">/</span>
                      <span className="text-slate-500">{agent.provider}</span>
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-800 tabular-nums">
                      {agent.telemetry.queue_depth}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-800 tabular-nums">
                      {agent.telemetry.success_rate_pct}%
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-slate-900 tabular-nums">
                      {agent.cost_today_formatted ?? `$${(agent.telemetry.cost_today_cents / 100).toFixed(2)}`}
                    </td>
                    {extraCols.includes('risk') && (
                      <td className="px-3 py-2.5">
                        <RiskText risk={agent.risk_level} />
                      </td>
                    )}
                    {extraCols.includes('latency') && (
                      <td className="px-3 py-2.5 text-right font-mono text-slate-600 tabular-nums">
                        {agent.telemetry.avg_latency_ms}ms
                      </td>
                    )}
                    {extraCols.includes('sla') && (
                      <td className="px-3 py-2.5 text-right font-mono text-slate-600 tabular-nums">
                        {agent.telemetry.sla_compliance_pct}%
                      </td>
                    )}
                    <td className="px-3 py-2.5">
                      <AgentHealthChip health={agent.health} />
                    </td>
                    <td className="relative px-3 py-2.5 text-right" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        aria-label={`Actions for ${agent.display_name}`}
                        onClick={() => setOpenRowMenu(openRowMenu === agent.agent_id ? null : agent.agent_id)}
                        className="rounded p-1 text-slate-500 hover:bg-slate-200/70 hover:text-slate-900"
                      >
                        <MoreHorizontal className="h-4 w-4" />
                      </button>

                      {openRowMenu === agent.agent_id && (
                        <div className="absolute right-4 top-9 z-30 w-44 rounded-lg border border-slate-200 bg-white py-1 text-left shadow-lg">
                          <button
                            type="button"
                            onClick={() => {
                              setOpenRowMenu(null);
                              onSelectAgent(agent.agent_id);
                            }}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
                          >
                            Open workspace
                          </button>
                          {agent.status === 'running' ? (
                            <button
                              type="button"
                              onClick={() => runRowQuickCommand(agent, 'pause')}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
                            >
                              <Pause className="h-3.5 w-3.5" /> Pause agent
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => runRowQuickCommand(agent, 'resume')}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-emerald-700 hover:bg-emerald-50"
                            >
                              <Play className="h-3.5 w-3.5" /> Resume agent
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => runRowQuickCommand(agent, 'safe_stop')}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-amber-700 hover:bg-amber-50"
                          >
                            Safe stop
                          </button>
                          <button
                            type="button"
                            onClick={() => runRowQuickCommand(agent, 'request_approval')}
                            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-violet-700 hover:bg-violet-50"
                          >
                            <ShieldCheck className="h-3.5 w-3.5" /> Request approval
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination & Bulk Actions Footer */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50/50 px-5 py-3 text-xs">
        <div className="flex flex-wrap items-center gap-2.5">
          {checkedIds.length > 0 ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-blue-200 bg-blue-50/70 px-3 py-1.5">
              <span className="font-semibold text-blue-900 tabular-nums">{checkedIds.length} selected</span>
              <span className="text-blue-200">|</span>
              <button
                type="button"
                disabled={bulkBusy}
                onClick={() => setShowBulkAssignModal(true)}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                <UserPlus className="h-3.5 w-3.5" /> Assign task
              </button>
              <button
                type="button"
                disabled={bulkBusy}
                onClick={() => runBulkAction('pause')}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                <Pause className="h-3.5 w-3.5" /> Pause
              </button>
              <button
                type="button"
                disabled={bulkBusy}
                onClick={() => runBulkAction('request_approval')}
                className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2.5 py-1 font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                <ShieldCheck className="h-3.5 w-3.5" /> Request approval
              </button>
              <button
                type="button"
                onClick={() => setCheckedIds([])}
                className="ml-1 text-slate-500 hover:text-slate-800"
                title="Clear selection"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <span className="text-slate-600 tabular-nums">
              Showing {startIdx}–{endIdx} of {totalAgents} agents
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-slate-600">
            Rows per page:
            <select
              value={filters.page_size}
              onChange={(e) => onFiltersChange({ page_size: Number(e.target.value), page: 1 })}
              className="rounded border border-slate-200 bg-white px-2 py-1 text-xs text-slate-800"
            >
              <option value={5}>5</option>
              <option value={10}>10</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
            </select>
          </label>

          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={filters.page <= 1}
              onClick={() => onFiltersChange({ page: filters.page - 1 })}
              aria-label="Previous page"
              className="rounded border border-slate-200 bg-white p-1 text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            {Array.from({ length: totalPages }).map((_, idx) => {
              const p = idx + 1;
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() => onFiltersChange({ page: p })}
                  className={`h-6 min-w-6 rounded px-2 font-mono text-xs font-medium tabular-nums ${
                    p === filters.page
                      ? 'bg-blue-600 text-white'
                      : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  {p}
                </button>
              );
            })}
            <button
              type="button"
              disabled={filters.page >= totalPages}
              onClick={() => onFiltersChange({ page: filters.page + 1 })}
              aria-label="Next page"
              className="rounded border border-slate-200 bg-white p-1 text-slate-600 hover:bg-slate-100 disabled:opacity-40"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* Register Agent Modal */}
      {showRegisterModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5">
              <h3 className="text-sm font-bold text-slate-900">Register Canonical Agent</h3>
              <button type="button" onClick={() => setShowRegisterModal(false)} className="text-slate-400 hover:text-slate-700">
                <X className="h-4 w-4" />
              </button>
            </div>
            <form onSubmit={handleRegisterAgent} className="space-y-3 p-5 text-xs">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Display name</label>
                  <input
                    required
                    value={regName}
                    onChange={(e) => setRegName(e.target.value)}
                    placeholder="e.g. Orion"
                    className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-slate-900 focus:border-blue-500 focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Operational role</label>
                  <input
                    required
                    value={regRole}
                    onChange={(e) => setRegRole(e.target.value)}
                    placeholder="e.g. Compliance"
                    className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-slate-900 focus:border-blue-500 focus:outline-none"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Team</label>
                  <select value={regTeam} onChange={(e) => setRegTeam(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5">
                    <option>Operations</option>
                    <option>Finance</option>
                    <option>Engineering</option>
                    <option>Communications</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Human supervisor</label>
                  <input value={regOwner} onChange={(e) => setRegOwner(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5" />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Provider</label>
                  <select value={regProvider} onChange={(e) => setRegProvider(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5">
                    <option>Google</option>
                    <option>OpenAI</option>
                    <option>Anthropic</option>
                    <option>Ollama</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Model</label>
                  <input value={regModel} onChange={(e) => setRegModel(e.target.value)} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5" />
                </div>
                <div>
                  <label className="mb-1 block font-medium text-slate-700">Risk tier</label>
                  <select value={regRisk} onChange={(e) => setRegRisk(e.target.value as 'low' | 'medium' | 'high')} className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5">
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                  </select>
                </div>
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowRegisterModal(false)} className="rounded-lg border border-slate-200 px-3 py-1.5 font-medium text-slate-700 hover:bg-slate-50">
                  Cancel
                </button>
                <button type="submit" disabled={regSubmitting} className="rounded-lg bg-blue-600 px-3.5 py-1.5 font-semibold text-white hover:bg-blue-700 disabled:opacity-50">
                  {regSubmitting ? 'Registering...' : 'Register agent'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Bulk Assign Task Modal */}
      {showBulkAssignModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5">
              <h3 className="text-sm font-bold text-slate-900">Assign Task to {checkedIds.length} Selected Agent(s)</h3>
              <button type="button" onClick={() => setShowBulkAssignModal(false)} className="text-slate-400 hover:text-slate-700">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-3 p-5 text-xs">
              <p className="text-slate-600">
                Affected agents: <span className="font-mono font-medium text-slate-900">{checkedIds.join(', ')}</span>
              </p>
              <div>
                <label className="mb-1 block font-medium text-slate-700">Task title</label>
                <input
                  value={bulkTaskTitle}
                  onChange={(e) => setBulkTaskTitle(e.target.value)}
                  placeholder="e.g. Audit Q4 vendor citations"
                  className="w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-slate-900 focus:border-blue-500 focus:outline-none"
                />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setShowBulkAssignModal(false)} className="rounded-lg border border-slate-200 px-3 py-1.5 font-medium text-slate-700">
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={bulkBusy || !bulkTaskTitle.trim()}
                  onClick={() => runBulkAction('assign_task', bulkTaskTitle.trim())}
                  className="rounded-lg bg-blue-600 px-3.5 py-1.5 font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  Confirm & Assign
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
