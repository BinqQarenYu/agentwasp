import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  PauseCircle,
  PlayCircle,
  ShieldAlert,
  WifiOff,
  XCircle,
} from 'lucide-react';
import type { AgentHealth, AgentStatus, RiskLevel } from '../shared/types';

const STATUS_CONFIG: Record<
  AgentStatus,
  { label: string; classes: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  running: {
    label: 'Working',
    classes: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    Icon: PlayCircle,
  },
  idle: {
    label: 'Available',
    classes: 'bg-sky-50 text-sky-700 border-sky-200',
    Icon: CheckCircle2,
  },
  blocked: {
    label: 'Blocked',
    classes: 'bg-rose-50 text-rose-700 border-rose-200',
    Icon: XCircle,
  },
  awaiting_approval: {
    label: 'Approval',
    classes: 'bg-violet-50 text-violet-700 border-violet-200',
    Icon: Clock,
  },
  paused: {
    label: 'Paused',
    classes: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: PauseCircle,
  },
  degraded: {
    label: 'Degraded',
    classes: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: AlertTriangle,
  },
  failed: {
    label: 'Failed',
    classes: 'bg-rose-50 text-rose-700 border-rose-200',
    Icon: ShieldAlert,
  },
  offline: {
    label: 'Offline',
    classes: 'bg-slate-100 text-slate-700 border-slate-300',
    Icon: WifiOff,
  },
  safe_stopping: {
    label: 'Stopping',
    classes: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: PauseCircle,
  },
  disabled: {
    label: 'Disabled',
    classes: 'bg-slate-100 text-slate-600 border-slate-200',
    Icon: XCircle,
  },
  registering: {
    label: 'Registering',
    classes: 'bg-blue-50 text-blue-700 border-blue-200',
    Icon: Clock,
  },
};

export function AgentStatusChip({ status }: { status: AgentStatus }) {
  const cfg = STATUS_CONFIG[status] ?? STATUS_CONFIG.idle;
  const Icon = cfg.Icon;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${cfg.classes}`}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{cfg.label}</span>
    </span>
  );
}

const HEALTH_CONFIG: Record<
  AgentHealth,
  { label: string; classes: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  healthy: {
    label: 'Healthy',
    classes: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    Icon: CheckCircle2,
  },
  api_issue: {
    label: 'API issue',
    classes: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: AlertTriangle,
  },
  degraded: {
    label: 'Degraded',
    classes: 'bg-amber-50 text-amber-800 border-amber-200',
    Icon: AlertTriangle,
  },
  stale: {
    label: 'Stale telemetry',
    classes: 'bg-slate-100 text-slate-700 border-slate-300',
    Icon: Clock,
  },
  offline: {
    label: 'Offline',
    classes: 'bg-rose-50 text-rose-700 border-rose-200',
    Icon: WifiOff,
  },
};

export function AgentHealthChip({ health }: { health: AgentHealth }) {
  const cfg = HEALTH_CONFIG[health] ?? HEALTH_CONFIG.stale;
  const Icon = cfg.Icon;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${cfg.classes}`}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{cfg.label}</span>
    </span>
  );
}

export function RiskText({ risk }: { risk: RiskLevel }) {
  const color =
    risk === 'critical'
      ? 'text-rose-700 font-semibold'
      : risk === 'high'
        ? 'text-amber-700 font-semibold'
        : risk === 'medium'
          ? 'text-slate-700 font-medium'
          : 'text-slate-500';
  return <span className={`text-xs capitalize ${color}`}>{risk}</span>;
}

export function formatShortTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}
