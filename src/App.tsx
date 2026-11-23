import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bell,
  CheckSquare,
  ChevronDown,
  FileSearch,
  GitBranch,
  Layers,
  Link2,
  Search,
  Settings,
  Users,
  X,
} from 'lucide-react';
import type {
  ActorRole,
  CanonicalAgent,
  IncidentRecord,
  LiveStreamEvent,
  TaskRecord,
  WorkforceSummary,
} from './shared/types';
import { apiRequest, getClientAuth, setClientAuth } from './api/client';
import { CommandCenter, type WorkforceFilters } from './components/CommandCenter';
import { AgentDetailWorkspace } from './components/AgentDetailWorkspace';
import { OrgHandoffView } from './components/OrgHandoffView';
import { SecondaryViews, type NavSection } from './components/SecondaryViews';

interface AgentsListResponse {
  items: Array<CanonicalAgent & { cost_today_formatted?: string }>;
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  facets: {
    teams: Array<{ id: string; name: string }>;
    owners: Array<{ id: string; name: string }>;
    providers: string[];
  };
}

const NAV_ITEMS: Array<{ id: NavSection; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: 'workforce', label: 'Workforce', icon: Users },
  { id: 'tasks', label: 'Tasks', icon: CheckSquare },
  { id: 'organization', label: 'Organization', icon: GitBranch },
  { id: 'knowledge', label: 'Knowledge / Graphify', icon: Layers },
  { id: 'integrations', label: 'Integrations', icon: Link2 },
  { id: 'incidents', label: 'Incidents', icon: AlertTriangle },
  { id: 'audit', label: 'Audit', icon: FileSearch },
  { id: 'settings', label: 'Settings', icon: Settings },
];

export function App() {
  const [nav, setNav] = useState<NavSection>('workforce');
  const [workspace, setWorkspace] = useState<string>('Operations');
  const [globalSearch, setGlobalSearch] = useState('');
  const [searchFocused, setSearchFocused] = useState(false);

  const [filters, setFilters] = useState<WorkforceFilters>({
    query: '',
    status: 'all',
    team_id: 'all',
    owner_id: 'all',
    provider: 'all',
    kpi_filter: 'all',
    sort: 'agent_id',
    sort_dir: 'asc',
    page: 1,
    page_size: 10,
  });

  const [summary, setSummary] = useState<WorkforceSummary | null>(null);
  const [agents, setAgents] = useState<Array<CanonicalAgent & { cost_today_formatted?: string }>>([]);
  const [allAgentsForRefs, setAllAgentsForRefs] = useState<CanonicalAgent[]>([]);
  const [allTasksForSearch, setAllTasksForSearch] = useState<TaskRecord[]>([]);
  const [openIncidents, setOpenIncidents] = useState<IncidentRecord[]>([]);
  const [totalAgents, setTotalAgents] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [facets, setFacets] = useState<AgentsListResponse['facets']>({
    teams: [],
    owners: [],
    providers: [],
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>('AG-001');
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [notifOpen, setNotifOpen] = useState(false);
  const [roleMenuOpen, setRoleMenuOpen] = useState(false);
  const [auth, setAuth] = useState(getClientAuth());
  const [toast, setToast] = useState<{ msg: string; tone: 'info' | 'success' | 'error' } | null>(null);

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastVersionByEntityRef = useRef<Map<string, number>>(new Map());

  const notify = useCallback((msg: string, tone: 'info' | 'success' | 'error' = 'info') => {
    setToast({ msg, tone });
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), 4500);
  }, []);

  const fetchWorkforce = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams();
      if (workspace !== 'All') qs.set('workspace', workspace);
      if (filters.query) qs.set('query', filters.query);
      if (filters.status !== 'all') qs.set('status', filters.status);
      if (filters.team_id !== 'all') qs.set('team_id', filters.team_id);
      if (filters.owner_id !== 'all') qs.set('owner_id', filters.owner_id);
      if (filters.provider !== 'all') qs.set('provider', filters.provider);
      if (filters.kpi_filter !== 'all') qs.set('kpi_filter', filters.kpi_filter);
      qs.set('sort', filters.sort);
      qs.set('sort_dir', filters.sort_dir);
      qs.set('page', String(filters.page));
      qs.set('page_size', String(filters.page_size));

      const summaryQs = workspace !== 'All' ? `?workspace=${encodeURIComponent(workspace)}` : '';
      const [sumRes, listRes, refRes, tasksRes, incRes] = await Promise.all([
        apiRequest<WorkforceSummary>(`/workforce/summary${summaryQs}`),
        apiRequest<AgentsListResponse>(`/agents?${qs.toString()}`),
        apiRequest<AgentsListResponse>('/agents?page=1&page_size=100'),
        apiRequest<{ items: TaskRecord[] }>('/tasks?status=all'),
        apiRequest<{ items: IncidentRecord[] }>('/incidents'),
      ]);

      setSummary(sumRes);
      setAgents(listRes.items);
      setTotalAgents(listRes.total);
      setTotalPages(listRes.total_pages);
      setFacets(listRes.facets);
      setAllAgentsForRefs(refRes.items);
      setAllTasksForSearch(tasksRes.items);
      setOpenIncidents(incRes.items.filter((i) => i.status !== 'resolved'));

      // Keep selected agent valid inside current tenant
      if (refRes.items.length > 0 && (!selectedAgentId || !refRes.items.some((a) => a.agent_id === selectedAgentId))) {
        setSelectedAgentId(refRes.items[0].agent_id);
      } else if (refRes.items.length === 0) {
        setSelectedAgentId(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workforce data');
    } finally {
      setLoading(false);
    }
  }, [workspace, filters, selectedAgentId]);

  useEffect(() => {
    void fetchWorkforce();
  }, [fetchWorkforce, refreshNonce, auth.tenantId, auth.actorRole]);

  // Live SSE synchronization — merges updates without resetting filters/pagination/selection
  useEffect(() => {
    const es = new EventSource('/api/v1/events/stream');
    es.onmessage = (evt) => {
      try {
        const parsed = JSON.parse(evt.data) as LiveStreamEvent;
        if (parsed.event_type === 'agent.heartbeat') return;
        const prevVer = lastVersionByEntityRef.current.get(parsed.entity_id) ?? 0;
        if (parsed.entity_version >= prevVer) {
          lastVersionByEntityRef.current.set(parsed.entity_id, parsed.entity_version);
          setRefreshNonce((n) => n + 1);
        }
      } catch {
        // Ignore non-JSON keepalive frames
      }
    };
    return () => {
      es.close();
    };
  }, [auth.tenantId]);

  const handleFiltersChange = useCallback((patch: Partial<WorkforceFilters>) => {
    setFilters((prev) => ({ ...prev, ...patch }));
  }, []);

  const triggerRefresh = useCallback(() => {
    setRefreshNonce((n) => n + 1);
  }, []);

  const handleSelectAgent = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
    if (nav !== 'workforce' && nav !== 'organization') {
      setNav('workforce');
    }
  }, [nav]);

  const switchRole = (role: ActorRole) => {
    const names: Record<ActorRole, string> = {
      supervisor: 'J. Lee',
      operator: 'M. Chen',
      auditor: 'R. Patel',
      viewer: 'Guest Analyst',
    };
    setClientAuth({ actorRole: role, actorName: names[role] });
    setAuth(getClientAuth());
    setRoleMenuOpen(false);
    setRefreshNonce((n) => n + 1);
    notify(`Active RBAC role switched to ${role} (${names[role]})`, 'info');
  };

  // Global search matches across agents & tasks
  const globalMatches = useMemo(() => {
    const q = globalSearch.trim().toLowerCase();
    if (!q) return { agents: [] as CanonicalAgent[], tasks: [] as TaskRecord[] };
    return {
      agents: allAgentsForRefs
        .filter(
          (a) =>
            a.display_name.toLowerCase().includes(q) ||
            a.agent_id.toLowerCase().includes(q) ||
            a.role.toLowerCase().includes(q) ||
            a.provider.toLowerCase().includes(q),
        )
        .slice(0, 5),
      tasks: allTasksForSearch
        .filter(
          (t) =>
            t.title.toLowerCase().includes(q) ||
            t.task_id.toLowerCase().includes(q) ||
            t.parent_goal.toLowerCase().includes(q),
        )
        .slice(0, 4),
    };
  }, [globalSearch, allAgentsForRefs, allTasksForSearch]);

  const pendingApprovalAgents = useMemo(
    () => allAgentsForRefs.filter((a) => a.status === 'awaiting_approval' || a.pending_approvals > 0),
    [allAgentsForRefs],
  );
  const notificationCount = pendingApprovalAgents.length + openIncidents.length;

  const initials = auth.actorName
    .split(/\s+/)
    .map((p) => p[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();

  return (
    <div className="min-h-screen bg-[#F4F7FB] text-slate-900">
      <div className="mx-auto flex min-h-screen max-w-[1680px]">
        {/* Left Navigation Sidebar */}
        <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-white lg:flex">
          <div className="flex items-center gap-2.5 border-b border-slate-200 px-4 py-4">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-600 text-white">
              <Layers className="h-4 w-4" />
            </div>
            <div className="leading-tight">
              <div className="font-display text-[11px] font-extrabold uppercase tracking-wider text-slate-900">
                Agent Workforce
              </div>
              <div className="font-display text-[10px] font-bold uppercase tracking-wider text-slate-500">
                Management
              </div>
            </div>
          </div>

          <nav aria-label="Primary navigation" className="flex-1 space-y-1 p-2.5">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = nav === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setNav(item.id)}
                  className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-[13px] font-medium transition ${
                    active
                      ? 'bg-blue-50 font-semibold text-blue-700'
                      : 'text-slate-700 hover:bg-slate-100 hover:text-slate-900'
                  }`}
                >
                  <span className="flex items-center gap-2.5">
                    <Icon className={`h-4 w-4 ${active ? 'text-blue-600' : 'text-slate-400'}`} />
                    {item.label}
                  </span>
                  {item.id === 'incidents' && openIncidents.length > 0 && (
                    <span className="rounded bg-red-100 px-1.5 py-0.5 font-mono text-[10px] font-bold text-red-700 tabular-nums">
                      {openIncidents.length}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          <div className="border-t border-slate-200 p-3 text-[11px] text-slate-500">
            <div className="font-semibold text-slate-700">Agent WASP Pool</div>
            <div className="mt-0.5 font-mono text-[10px] text-slate-500">{auth.tenantId}</div>
          </div>
        </aside>

        {/* Main Content Shell */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* Top Bar */}
          <header className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-5 py-3">
            <div className="flex items-center gap-3">
              {/* Mobile Nav Selector */}
              <select
                aria-label="Mobile section selector"
                value={nav}
                onChange={(e) => setNav(e.target.value as NavSection)}
                className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-800 lg:hidden"
              >
                {NAV_ITEMS.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.label}
                  </option>
                ))}
              </select>

              <label className="flex items-center gap-2 text-xs font-medium text-slate-500">
                <span>Workspace:</span>
                <select
                  aria-label="Select workspace"
                  value={workspace}
                  onChange={(e) => {
                    setWorkspace(e.target.value);
                    setFilters((f) => ({ ...f, page: 1 }));
                  }}
                  className="rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-semibold text-slate-900 focus:border-blue-500 focus:outline-none"
                >
                  <option value="Operations">Operations</option>
                  <option value="All">All Workspaces</option>
                </select>
              </label>
            </div>

            {/* Global Search */}
            <div className="relative flex-1 max-w-lg">
              <Search className="pointer-events-none absolute left-3 top-2 h-4 w-4 text-slate-400" />
              <input
                type="search"
                aria-label="Search across agents, tasks, knowledge"
                value={globalSearch}
                onFocus={() => setSearchFocused(true)}
                onBlur={() => setTimeout(() => setSearchFocused(false), 180)}
                onChange={(e) => setGlobalSearch(e.target.value)}
                placeholder="Search across agents, tasks, knowledge..."
                className="w-full rounded-lg border border-slate-200 bg-slate-50 py-1.5 pl-9 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none"
              />
              {searchFocused && globalSearch.trim().length > 0 && (
                <div className="absolute left-0 right-0 top-9 z-30 rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
                  {globalMatches.agents.length === 0 && globalMatches.tasks.length === 0 ? (
                    <div className="px-2 py-2 text-xs text-slate-500">No matching agents or tasks in current tenant.</div>
                  ) : (
                    <div className="space-y-2">
                      {globalMatches.agents.length > 0 && (
                        <div>
                          <div className="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                            Agents
                          </div>
                          {globalMatches.agents.map((a) => (
                            <button
                              key={a.agent_id}
                              type="button"
                              onMouseDown={() => {
                                handleSelectAgent(a.agent_id);
                                setGlobalSearch('');
                              }}
                              className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-blue-50"
                            >
                              <span className="font-semibold text-slate-900">
                                {a.display_name} <span className="font-normal text-slate-500">/ {a.role}</span>
                              </span>
                              <span className="font-mono text-[11px] text-slate-500">{a.agent_id}</span>
                            </button>
                          ))}
                        </div>
                      )}
                      {globalMatches.tasks.length > 0 && (
                        <div>
                          <div className="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                            Tasks
                          </div>
                          {globalMatches.tasks.map((t) => (
                            <button
                              key={t.task_id}
                              type="button"
                              onMouseDown={() => {
                                setNav('tasks');
                                setGlobalSearch('');
                              }}
                              className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-blue-50"
                            >
                              <span className="font-medium text-slate-800">{t.title}</span>
                              <span className="font-mono text-[11px] text-slate-500">{t.task_id}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Right Controls: Notifications & Profile */}
            <div className="flex items-center gap-3">
              {/* Notifications Bell */}
              <div className="relative">
                <button
                  type="button"
                  aria-label="Open notifications"
                  onClick={() => setNotifOpen((o) => !o)}
                  className="relative rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 hover:bg-slate-50"
                >
                  <Bell className="h-4 w-4" />
                  {notificationCount > 0 && (
                    <span className="absolute -right-1 -top-1 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-blue-600 px-1 font-mono text-[10px] font-bold text-white tabular-nums">
                      {notificationCount}
                    </span>
                  )}
                </button>
                {notifOpen && (
                  <div className="absolute right-0 top-10 z-30 w-80 rounded-xl border border-slate-200 bg-white p-3 shadow-lg">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-xs font-bold text-slate-900">Action Queue ({notificationCount})</span>
                      <button
                        type="button"
                        onClick={() => setNotifOpen(false)}
                        className="text-slate-400 hover:text-slate-600"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div className="max-h-72 space-y-2 overflow-y-auto text-xs">
                      {pendingApprovalAgents.map((a) => (
                        <button
                          key={`apr-${a.agent_id}`}
                          type="button"
                          onClick={() => {
                            setNotifOpen(false);
                            handleSelectAgent(a.agent_id);
                          }}
                          className="w-full rounded-lg border border-violet-200 bg-violet-50/70 p-2.5 text-left hover:bg-violet-100/70"
                        >
                          <div className="font-semibold text-violet-900">Approval required: {a.display_name}</div>
                          <div className="mt-0.5 text-[11px] text-violet-700">
                            {a.current_task_title || 'Governed action'} · Click to review
                          </div>
                        </button>
                      ))}
                      {openIncidents.map((inc) => (
                        <button
                          key={inc.incident_id}
                          type="button"
                          onClick={() => {
                            setNotifOpen(false);
                            setNav('incidents');
                          }}
                          className="w-full rounded-lg border border-red-200 bg-red-50/70 p-2.5 text-left hover:bg-red-100/70"
                        >
                          <div className="font-semibold text-red-900">Incident: {inc.title}</div>
                          <div className="mt-0.5 text-[11px] text-red-700">
                            {inc.agent_id} · Severity {inc.severity}
                          </div>
                        </button>
                      ))}
                      {notificationCount === 0 && (
                        <div className="py-4 text-center text-slate-500">All approvals and incidents are clear.</div>
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* User Profile / Role Switcher */}
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setRoleMenuOpen((o) => !o)}
                  className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs hover:bg-slate-50"
                >
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-200 font-semibold text-slate-700">
                    {initials}
                  </span>
                  <div className="text-left leading-tight">
                    <div className="font-semibold text-slate-900">{auth.actorName}</div>
                    <div className="text-[10px] capitalize text-slate-500">{auth.actorRole}</div>
                  </div>
                  <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
                </button>
                {roleMenuOpen && (
                  <div className="absolute right-0 top-11 z-30 w-48 rounded-lg border border-slate-200 bg-white p-1.5 shadow-lg">
                    <div className="px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                      Switch Active Role
                    </div>
                    {(['supervisor', 'operator', 'auditor', 'viewer'] as ActorRole[]).map((role) => (
                      <button
                        key={role}
                        type="button"
                        onClick={() => switchRole(role)}
                        className={`flex w-full items-center justify-between rounded px-2.5 py-1.5 text-xs capitalize ${
                          auth.actorRole === role
                            ? 'bg-blue-50 font-semibold text-blue-700'
                            : 'text-slate-700 hover:bg-slate-50'
                        }`}
                      >
                        <span>{role}</span>
                        {auth.actorRole === role && <span className="text-[10px]">Active</span>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </header>

          {/* Toast Feedback Banner */}
          {toast && (
            <div className="px-5 pt-3">
              <div
                role="status"
                className={`flex items-center justify-between rounded-lg border px-3.5 py-2 text-xs font-medium ${
                  toast.tone === 'error'
                    ? 'border-red-200 bg-red-50 text-red-800'
                    : toast.tone === 'success'
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                      : 'border-blue-200 bg-blue-50 text-blue-800'
                }`}
              >
                <span>{toast.msg}</span>
                <button type="button" onClick={() => setToast(null)} className="ml-3 opacity-70 hover:opacity-100">
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          )}

          {/* Main Workspace Canvas */}
          <main className="flex-1 space-y-4 p-5">
            {nav === 'workforce' && (
              <>
                <CommandCenter
                  summary={summary}
                  agents={agents}
                  totalAgents={totalAgents}
                  totalPages={totalPages}
                  facets={facets}
                  filters={filters}
                  loading={loading}
                  error={error}
                  selectedAgentId={selectedAgentId}
                  onFiltersChange={handleFiltersChange}
                  onSelectAgent={setSelectedAgentId}
                  onRefresh={triggerRefresh}
                  onNotify={notify}
                />

                <div className="grid gap-4 xl:grid-cols-12">
                  <div className="xl:col-span-7">
                    <AgentDetailWorkspace
                      agentId={selectedAgentId}
                      refreshNonce={refreshNonce}
                      onSelectAgent={setSelectedAgentId}
                      onAgentMutated={triggerRefresh}
                      onNotify={notify}
                    />
                  </div>
                  <div className="xl:col-span-5">
                    <OrgHandoffView
                      teams={facets.teams}
                      agents={allAgentsForRefs}
                      selectedAgentId={selectedAgentId}
                      refreshNonce={refreshNonce}
                      onSelectAgent={setSelectedAgentId}
                      onMutated={triggerRefresh}
                      onNotify={notify}
                    />
                  </div>
                </div>
              </>
            )}

            {nav === 'organization' && (
              <div className="space-y-4">
                <OrgHandoffView
                  teams={facets.teams}
                  agents={allAgentsForRefs}
                  selectedAgentId={selectedAgentId}
                  refreshNonce={refreshNonce}
                  expanded
                  onSelectAgent={setSelectedAgentId}
                  onMutated={triggerRefresh}
                  onNotify={notify}
                />
                <AgentDetailWorkspace
                  agentId={selectedAgentId}
                  refreshNonce={refreshNonce}
                  onSelectAgent={setSelectedAgentId}
                  onAgentMutated={triggerRefresh}
                  onNotify={notify}
                />
              </div>
            )}

            {nav !== 'workforce' && nav !== 'organization' && (
              <SecondaryViews
                section={nav}
                agents={allAgentsForRefs}
                refreshNonce={refreshNonce}
                onSelectAgent={handleSelectAgent}
                onMutated={triggerRefresh}
                onAuthChanged={() => {
                  setAuth(getClientAuth());
                  triggerRefresh();
                }}
                onNotify={notify}
              />
            )}
          </main>

          {/* Footer Design Principles Strip */}
          <footer className="border-t border-slate-200 bg-white px-5 py-2.5 text-xs text-slate-600">
            <span className="font-semibold text-slate-800">Design principles:</span> one canonical agent record · role-based controls · auditable actions · live data with explicit stale states
          </footer>
        </div>
      </div>
    </div>
  );
}

export default App;
