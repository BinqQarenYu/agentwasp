import React, { useMemo, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Activity, Clock, GitCompare, TrendingUp, X } from 'lucide-react';
import type { AgentPerformancePoint } from '../shared/types';

export type PerformanceChartMode = 'efficiency' | 'latency' | 'both';

export interface CompareCandidate {
  agent_id: string;
  display_name: string;
  role: string;
}

interface MergedPerformancePoint extends AgentPerformancePoint {
  compare_efficiency_pct?: number;
  compare_avg_latency_ms?: number;
  compare_tasks_completed?: number;
  compare_tasks_failed?: number;
}

interface AgentPerformanceChartProps {
  data: AgentPerformancePoint[];
  agentName: string;
  telemetryAvailable?: boolean;
  compact?: boolean;
  defaultMode?: PerformanceChartMode;
  compareCandidates?: CompareCandidate[];
  compareAgentId?: string | null;
  compareAgentName?: string | null;
  compareData?: AgentPerformancePoint[];
  compareLoading?: boolean;
  onCompareChange?: (agentId: string | null) => void;
  onOpenQualityTab?: () => void;
}

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{
    dataKey?: string;
    value?: number;
    payload?: MergedPerformancePoint;
  }>;
  agentName: string;
  compareAgentName?: string | null;
}

function ChartTooltip({ active, payload, agentName, compareAgentName }: CustomTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const pt = payload[0]?.payload;
  if (!pt) return null;

  const hasCompare = Boolean(compareAgentName && typeof pt.compare_efficiency_pct === 'number');
  const effGap = hasCompare ? Number((pt.efficiency_pct - pt.compare_efficiency_pct!).toFixed(1)) : null;

  return (
    <div className="min-w-[215px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-md">
      <div className="mb-1.5 font-semibold text-slate-900">
        {pt.label} <span className="font-mono text-[10px] font-normal text-slate-400">({pt.date})</span>
      </div>
      <div className="space-y-1 tabular-nums">
        <div className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-slate-600">
            <span className="inline-block h-2 w-2 rounded-full bg-blue-600" />
            {agentName} eff.
          </span>
          <span className="font-mono font-semibold text-slate-900">{pt.efficiency_pct.toFixed(1)}%</span>
        </div>

        {hasCompare && (
          <div className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-slate-600">
              <span className="inline-block h-2 w-2 rounded-full bg-violet-600" />
              {compareAgentName} eff.
            </span>
            <span className="font-mono font-semibold text-violet-700">
              {pt.compare_efficiency_pct!.toFixed(1)}%
              {effGap !== null && (
                <span className={`ml-1 text-[10px] ${effGap >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                  ({effGap >= 0 ? '+' : ''}
                  {effGap}%)
                </span>
              )}
            </span>
          </div>
        )}

        <div className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-slate-600">
            <span className="inline-block h-2 w-2 rounded-full bg-amber-600" />
            {agentName} latency
          </span>
          <span className="font-mono font-semibold text-slate-900">{pt.avg_latency_ms} ms</span>
        </div>

        {hasCompare && typeof pt.compare_avg_latency_ms === 'number' && (
          <div className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 text-slate-600">
              <span className="inline-block h-2 w-2 rounded-full bg-teal-600" />
              {compareAgentName} latency
            </span>
            <span className="font-mono font-semibold text-teal-700">{pt.compare_avg_latency_ms} ms</span>
          </div>
        )}

        <div className="mt-1 border-t border-slate-100 pt-1 text-[11px] text-slate-500">
          {agentName}: <strong className="font-mono text-slate-800">{pt.tasks_completed}</strong> ok /{' '}
          <strong className="font-mono text-slate-800">{pt.tasks_failed}</strong> fail
          {hasCompare && typeof pt.compare_tasks_completed === 'number' && (
            <div>
              {compareAgentName}: <strong className="font-mono text-slate-800">{pt.compare_tasks_completed}</strong> ok /{' '}
              <strong className="font-mono text-slate-800">{pt.compare_tasks_failed ?? 0}</strong> fail
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function AgentPerformanceChart({
  data,
  agentName,
  telemetryAvailable = true,
  compact = false,
  defaultMode = 'efficiency',
  compareCandidates = [],
  compareAgentId = null,
  compareAgentName = null,
  compareData = [],
  compareLoading = false,
  onCompareChange,
  onOpenQualityTab,
}: AgentPerformanceChartProps) {
  const [mode, setMode] = useState<PerformanceChartMode>(defaultMode);

  const hasComparison = Boolean(compareAgentId && compareAgentName && compareData.length > 0);

  const chartData = useMemo<MergedPerformancePoint[]>(() => {
    if (!hasComparison) return data;
    const compareByDate = new Map(compareData.map((pt) => [pt.date, pt]));
    return data.map((pt, idx) => {
      const match = compareByDate.get(pt.date) ?? compareData[idx];
      if (!match) return pt;
      return {
        ...pt,
        compare_efficiency_pct: match.efficiency_pct,
        compare_avg_latency_ms: match.avg_latency_ms,
        compare_tasks_completed: match.tasks_completed,
        compare_tasks_failed: match.tasks_failed,
      };
    });
  }, [data, compareData, hasComparison]);

  const stats = useMemo(() => {
    if (data.length === 0) {
      return {
        avgEff: 0,
        avgLatency: 0,
        effDelta: 0,
        latencyDelta: 0,
        compareAvgEff: null as number | null,
        compareAvgLatency: null as number | null,
        minEff: 80,
        maxLatency: 600,
      };
    }

    const avgEff = data.reduce((sum, d) => sum + d.efficiency_pct, 0) / data.length;
    const avgLatency = Math.round(data.reduce((sum, d) => sum + d.avg_latency_ms, 0) / data.length);

    const recent7 = data.slice(-7);
    const prior7 = data.slice(-14, -7);
    const recentEff = recent7.reduce((s, d) => s + d.efficiency_pct, 0) / Math.max(1, recent7.length);
    const priorEff = prior7.reduce((s, d) => s + d.efficiency_pct, 0) / Math.max(1, prior7.length);
    const recentLat = recent7.reduce((s, d) => s + d.avg_latency_ms, 0) / Math.max(1, recent7.length);
    const priorLat = prior7.reduce((s, d) => s + d.avg_latency_ms, 0) / Math.max(1, prior7.length);

    const effValues = data.map((d) => d.efficiency_pct);
    const latValues = data.map((d) => d.avg_latency_ms);

    let compareAvgEff: number | null = null;
    let compareAvgLatency: number | null = null;
    if (hasComparison) {
      compareAvgEff = Number(
        (compareData.reduce((sum, d) => sum + d.efficiency_pct, 0) / compareData.length).toFixed(1),
      );
      compareAvgLatency = Math.round(
        compareData.reduce((sum, d) => sum + d.avg_latency_ms, 0) / compareData.length,
      );
      effValues.push(...compareData.map((d) => d.efficiency_pct));
      latValues.push(...compareData.map((d) => d.avg_latency_ms));
    }

    const minEffRaw = Math.min(...effValues);
    const maxLatRaw = Math.max(...latValues);

    return {
      avgEff: Number(avgEff.toFixed(1)),
      avgLatency,
      effDelta: Number((recentEff - priorEff).toFixed(1)),
      latencyDelta: Math.round(recentLat - priorLat),
      compareAvgEff,
      compareAvgLatency,
      minEff: Math.max(0, Math.floor((minEffRaw - 4) / 5) * 5),
      maxLatency: Math.max(200, Math.ceil((maxLatRaw + 60) / 100) * 100),
    };
  }, [data, compareData, hasComparison]);

  const modes: Array<{ id: PerformanceChartMode; label: string }> = compact
    ? [
        { id: 'efficiency', label: 'Efficiency' },
        { id: 'latency', label: 'Latency' },
      ]
    : [
        { id: 'efficiency', label: 'Efficiency (%)' },
        { id: 'latency', label: 'Latency (ms)' },
        { id: 'both', label: 'Both' },
      ];

  const effSpread =
    stats.compareAvgEff !== null ? Number((stats.avgEff - stats.compareAvgEff).toFixed(1)) : null;
  const latSpread =
    stats.compareAvgLatency !== null ? stats.avgLatency - stats.compareAvgLatency : null;

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3.5">
      {/* Header Row */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-4 w-4 text-blue-600" />
          <div>
            <div className="font-semibold text-slate-900">
              30-Day Performance Trends
              {!compact && <span className="ml-1.5 font-normal text-slate-500">· {agentName}</span>}
            </div>
            <div className="flex flex-wrap items-center gap-3 text-[11px] text-slate-500 tabular-nums">
              <span className="inline-flex items-center gap-1">
                <Activity className="h-3 w-3 text-blue-600" />
                30d avg: <strong className="font-mono text-slate-800">{stats.avgEff}%</strong>
                <span className={`font-mono ${stats.effDelta >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                  ({stats.effDelta >= 0 ? '+' : ''}
                  {stats.effDelta}%)
                </span>
              </span>
              <span className="inline-flex items-center gap-1">
                <Clock className="h-3 w-3 text-amber-600" />
                Avg latency: <strong className="font-mono text-slate-800">{stats.avgLatency} ms</strong>
                <span className={`font-mono ${stats.latencyDelta <= 0 ? 'text-emerald-700' : 'text-amber-700'}`}>
                  ({stats.latencyDelta > 0 ? '+' : ''}
                  {stats.latencyDelta} ms)
                </span>
              </span>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Comparison Agent Selector */}
          {onCompareChange && compareCandidates.length > 0 && (
            <div className="inline-flex items-center gap-1">
              <GitCompare className="h-3.5 w-3.5 text-violet-600" aria-hidden="true" />
              <select
                aria-label="Compare with another agent"
                value={compareAgentId ?? ''}
                onChange={(e) => onCompareChange(e.target.value || null)}
                className="max-w-[165px] rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 focus:border-violet-500 focus:outline-none"
              >
                <option value="">Compare agent…</option>
                {compareCandidates.map((c) => (
                  <option key={c.agent_id} value={c.agent_id}>
                    {c.display_name} ({c.role})
                  </option>
                ))}
              </select>
              {compareAgentId && (
                <button
                  type="button"
                  aria-label="Clear comparison agent"
                  onClick={() => onCompareChange(null)}
                  className="rounded border border-slate-200 p-0.5 text-slate-400 hover:bg-slate-50 hover:text-slate-700"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          )}

          <div
            role="tablist"
            aria-label="Performance trend metric"
            className="inline-flex rounded-md border border-slate-200 bg-slate-50 p-0.5"
          >
            {modes.map((m) => (
              <button
                key={m.id}
                type="button"
                role="tab"
                aria-selected={mode === m.id}
                onClick={() => setMode(m.id)}
                className={`rounded px-2 py-0.5 text-[11px] font-semibold transition ${
                  mode === m.id ? 'bg-blue-600 text-white' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>

          {compact && onOpenQualityTab && (
            <button
              type="button"
              onClick={onOpenQualityTab}
              className="text-[11px] font-semibold text-blue-600 hover:underline"
            >
              Quality tab
            </button>
          )}
        </div>
      </div>

      {/* Active Comparison Delta Banner */}
      {compareAgentId && (
        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-md border border-violet-200 bg-violet-50/60 px-2.5 py-1.5 text-[11px] text-violet-950">
          {compareLoading ? (
            <span className="text-violet-700">Loading 30-day metrics for comparison agent…</span>
          ) : hasComparison && effSpread !== null && latSpread !== null ? (
            <>
              <div className="flex flex-wrap items-center gap-2 tabular-nums">
                <span className="font-semibold text-violet-900">
                  {agentName} vs {compareAgentName}
                </span>
                <span className="text-violet-300">|</span>
                <span>
                  Efficiency:{' '}
                  <strong className="font-mono text-slate-900">{stats.avgEff}%</strong> vs{' '}
                  <strong className="font-mono text-violet-800">{stats.compareAvgEff}%</strong>{' '}
                  <span className={`font-mono font-semibold ${effSpread >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                    ({effSpread >= 0 ? '+' : ''}
                    {effSpread}%)
                  </span>
                </span>
                <span className="text-violet-300">·</span>
                <span>
                  Latency:{' '}
                  <strong className="font-mono text-slate-900">{stats.avgLatency}ms</strong> vs{' '}
                  <strong className="font-mono text-teal-800">{stats.compareAvgLatency}ms</strong>{' '}
                  <span className={`font-mono font-semibold ${latSpread <= 0 ? 'text-emerald-700' : 'text-amber-700'}`}>
                    ({latSpread > 0 ? '+' : ''}
                    {latSpread}ms)
                  </span>
                </span>
              </div>
              {onCompareChange && (
                <button
                  type="button"
                  onClick={() => onCompareChange(null)}
                  className="font-semibold text-violet-700 hover:underline"
                >
                  Clear
                </button>
              )}
            </>
          ) : null}
        </div>
      )}

      {!telemetryAvailable && (
        <div className="mt-2.5 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-900">
          Live telemetry heartbeat is currently stale; displaying last recorded 30-day history.
        </div>
      )}

      {/* Recharts Line Chart */}
      <div className={`mt-3 w-full min-w-0 ${compact ? 'h-44' : 'h-60'}`}>
        {chartData.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-slate-500">
            No 30-day performance points recorded yet.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 8, right: mode === 'both' ? 12 : 10, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#E2E8F0" />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 10, fill: '#64748B' }}
                tickLine={false}
                axisLine={{ stroke: '#E2E8F0' }}
                minTickGap={22}
              />

              {(mode === 'efficiency' || mode === 'both') && (
                <YAxis
                  yAxisId="eff"
                  domain={[stats.minEff, 100]}
                  tickFormatter={(v: number) => `${v}%`}
                  tick={{ fontSize: 10, fill: '#64748B' }}
                  tickLine={false}
                  axisLine={false}
                  width={44}
                />
              )}

              {(mode === 'latency' || mode === 'both') && (
                <YAxis
                  yAxisId="lat"
                  orientation={mode === 'both' ? 'right' : 'left'}
                  domain={[0, stats.maxLatency]}
                  tickFormatter={(v: number) => `${v}ms`}
                  tick={{ fontSize: 10, fill: '#64748B' }}
                  tickLine={false}
                  axisLine={false}
                  width={mode === 'both' ? 48 : 52}
                />
              )}

              <Tooltip content={<ChartTooltip agentName={agentName} compareAgentName={compareAgentName} />} />

              {(mode === 'efficiency' || mode === 'both') && (
                <ReferenceLine
                  yAxisId="eff"
                  y={95}
                  stroke="#94A3B8"
                  strokeDasharray="4 4"
                  label={
                    !compact
                      ? {
                          value: '95% SLA',
                          position: 'insideTopRight',
                          fill: '#64748B',
                          fontSize: 10,
                        }
                      : undefined
                  }
                />
              )}

              {/* Primary Agent Efficiency */}
              {(mode === 'efficiency' || mode === 'both') && (
                <Line
                  yAxisId="eff"
                  type="monotone"
                  dataKey="efficiency_pct"
                  name={`${agentName} efficiency (%)`}
                  stroke="#2563EB"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: '#2563EB', stroke: '#FFFFFF', strokeWidth: 2 }}
                  isAnimationActive={false}
                />
              )}

              {/* Comparison Agent Efficiency Overlay */}
              {hasComparison && (mode === 'efficiency' || mode === 'both') && (
                <Line
                  yAxisId="eff"
                  type="monotone"
                  dataKey="compare_efficiency_pct"
                  name={`${compareAgentName} efficiency (%)`}
                  stroke="#7C3AED"
                  strokeWidth={2}
                  strokeDasharray="5 3"
                  dot={false}
                  activeDot={{ r: 4, fill: '#7C3AED', stroke: '#FFFFFF', strokeWidth: 2 }}
                  isAnimationActive={false}
                />
              )}

              {/* Primary Agent Latency */}
              {(mode === 'latency' || mode === 'both') && (
                <Line
                  yAxisId="lat"
                  type="monotone"
                  dataKey="avg_latency_ms"
                  name={`${agentName} latency (ms)`}
                  stroke="#D97706"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: '#D97706', stroke: '#FFFFFF', strokeWidth: 2 }}
                  isAnimationActive={false}
                />
              )}

              {/* Comparison Agent Latency Overlay (in Latency mode) */}
              {hasComparison && mode === 'latency' && (
                <Line
                  yAxisId="lat"
                  type="monotone"
                  dataKey="compare_avg_latency_ms"
                  name={`${compareAgentName} latency (ms)`}
                  stroke="#0D9488"
                  strokeWidth={2}
                  strokeDasharray="5 3"
                  dot={false}
                  activeDot={{ r: 4, fill: '#0D9488', stroke: '#FFFFFF', strokeWidth: 2 }}
                  isAnimationActive={false}
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Inline Legend */}
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-2 text-[11px] text-slate-500">
        <div className="flex flex-wrap items-center gap-3">
          {(mode === 'efficiency' || mode === 'both') && (
            <span className="inline-flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-4 bg-blue-600" />
              {agentName} efficiency (%)
            </span>
          )}
          {hasComparison && (mode === 'efficiency' || mode === 'both') && (
            <span className="inline-flex items-center gap-1.5 text-violet-700">
              <span className="inline-block w-4 border-t-2 border-dashed border-violet-600" />
              {compareAgentName} efficiency (%)
            </span>
          )}
          {(mode === 'latency' || mode === 'both') && (
            <span className="inline-flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-4 bg-amber-600" />
              {agentName} latency (ms)
            </span>
          )}
          {hasComparison && mode === 'latency' && (
            <span className="inline-flex items-center gap-1.5 text-teal-700">
              <span className="inline-block w-4 border-t-2 border-dashed border-teal-600" />
              {compareAgentName} latency (ms)
            </span>
          )}
        </div>
        <span className="font-mono text-[10px] text-slate-400">Window: Last 30 days (daily)</span>
      </div>
    </div>
  );
}
