import crypto from 'node:crypto';
import type { Request } from 'express';
import type { ActorRole } from '../shared/types.ts';

export interface RequestContext {
  tenant_id: string;
  actor_id: string;
  actor_name: string;
  actor_role: ActorRole;
  correlation_id: string;
}

const VALID_ROLES: Set<ActorRole> = new Set(['supervisor', 'operator', 'auditor', 'viewer']);

const ROLE_PERMISSIONS: Record<ActorRole, Set<string>> = {
  supervisor: new Set([
    'agent:read',
    'agent:write',
    'agent:control',
    'memory:read_restricted',
    'approval:decide',
    'connector:manage',
    'audit:read',
    'export:run',
  ]),
  operator: new Set([
    'agent:read',
    'agent:write',
    'agent:control',
    'connector:manage',
    'audit:read',
    'export:run',
  ]),
  auditor: new Set(['agent:read', 'audit:read', 'export:run']),
  viewer: new Set(['agent:read']),
};

export function extractContext(req: Request): RequestContext {
  const tenantRaw = String(req.header('x-tenant-id') || 'tenant-wasp-ops').trim();
  const tenant_id = /^[a-zA-Z0-9_-]{3,64}$/.test(tenantRaw) ? tenantRaw : 'tenant-wasp-ops';

  const roleRaw = String(req.header('x-actor-role') || 'supervisor').trim().toLowerCase() as ActorRole;
  const actor_role: ActorRole = VALID_ROLES.has(roleRaw) ? roleRaw : 'supervisor';

  const actor_id = String(req.header('x-actor-id') || 'usr-jlee').trim().slice(0, 64) || 'usr-jlee';
  const actor_name = String(req.header('x-actor-name') || 'J. Lee').trim().slice(0, 80) || 'J. Lee';
  const correlation_id =
    String(req.header('x-correlation-id') || '').trim().slice(0, 64) ||
    `corr_${crypto.randomBytes(6).toString('hex')}`;

  return { tenant_id, actor_id, actor_name, actor_role, correlation_id };
}

export function hasPermission(role: ActorRole, permission: string): boolean {
  return ROLE_PERMISSIONS[role]?.has(permission) ?? false;
}

const SECRET_PATTERNS = [
  /AIza[0-9A-Za-z_-]{20,}/g,
  /sk-[0-9A-Za-z_-]{16,}/g,
  /ghp_[0-9A-Za-z]{20,}/g,
  /xai-[0-9A-Za-z_-]{16,}/g,
  /(password|secret|token|api_key)\s*[:=]\s*[^\s,;]+/gi,
];

export function redactSecrets(input: string): string {
  let out = input;
  for (const pattern of SECRET_PATTERNS) {
    out = out.replace(pattern, '[REDACTED]');
  }
  return out;
}

export function formatCentsUsd(cents: number): string {
  const negative = cents < 0;
  const abs = Math.abs(Math.round(cents));
  const dollars = Math.floor(abs / 100);
  const rem = abs % 100;
  return `${negative ? '-' : ''}$${dollars.toLocaleString('en-US')}.${String(rem).padStart(2, '0')}`;
}
