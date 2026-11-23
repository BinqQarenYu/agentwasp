import type { AgentCommandType, AgentStatus, TaskStatus } from '../shared/types.ts';

export const ALLOWED_AGENT_TRANSITIONS: Record<AgentStatus, AgentStatus[]> = {
  registering: ['idle', 'failed', 'disabled'],
  idle: ['running', 'paused', 'awaiting_approval', 'safe_stopping', 'failed', 'offline', 'disabled'],
  running: ['idle', 'paused', 'blocked', 'degraded', 'awaiting_approval', 'safe_stopping', 'failed', 'offline', 'disabled'],
  paused: ['running', 'idle', 'safe_stopping', 'disabled', 'offline', 'failed'],
  blocked: ['running', 'idle', 'paused', 'awaiting_approval', 'safe_stopping', 'failed', 'offline', 'disabled'],
  degraded: ['running', 'idle', 'paused', 'blocked', 'safe_stopping', 'failed', 'offline', 'disabled'],
  awaiting_approval: ['running', 'idle', 'paused', 'blocked', 'safe_stopping', 'disabled', 'failed'],
  safe_stopping: ['idle', 'paused', 'failed', 'offline', 'disabled'],
  failed: ['idle', 'running', 'disabled', 'offline'],
  offline: ['idle', 'running', 'disabled'],
  disabled: ['idle'],
};

export const ALLOWED_TASK_TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  draft: ['queued', 'assigned', 'cancelled'],
  queued: ['assigned', 'running', 'cancelled'],
  assigned: ['running', 'blocked', 'awaiting_approval', 'cancelled'],
  running: ['blocked', 'awaiting_approval', 'retry_scheduled', 'completed', 'failed', 'cancelled'],
  blocked: ['running', 'awaiting_approval', 'retry_scheduled', 'failed', 'cancelled'],
  awaiting_approval: ['running', 'blocked', 'cancelled', 'failed'],
  retry_scheduled: ['running', 'blocked', 'failed', 'cancelled'],
  completed: [],
  failed: ['retry_scheduled', 'queued', 'cancelled'],
  cancelled: [],
};

export function resolveCommandTargetStatus(
  current: AgentStatus,
  command: AgentCommandType,
): AgentStatus | null {
  switch (command) {
    case 'pause':
      return 'paused';
    case 'resume':
      return current === 'paused' || current === 'blocked' || current === 'idle' || current === 'awaiting_approval'
        ? 'running'
        : null;
    case 'safe_stop':
      return 'idle';
    case 'cancel':
      return 'idle';
    case 'retry':
      return current === 'failed' || current === 'blocked' || current === 'degraded' || current === 'offline'
        ? 'running'
        : null;
    case 'disable':
      return 'disabled';
    case 'rollback':
      return 'idle';
    case 'request_approval':
      return 'awaiting_approval';
    default:
      return null;
  }
}

export function isValidAgentTransition(from: AgentStatus, to: AgentStatus): boolean {
  if (from === to) return false;
  return (ALLOWED_AGENT_TRANSITIONS[from] ?? []).includes(to);
}

export function isValidTaskTransition(from: TaskStatus, to: TaskStatus): boolean {
  if (from === to) return false;
  return (ALLOWED_TASK_TRANSITIONS[from] ?? []).includes(to);
}
