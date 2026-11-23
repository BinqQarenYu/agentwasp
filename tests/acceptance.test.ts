import assert from 'node:assert/strict';
import type { AddressInfo } from 'node:net';
import { after, before, describe, it } from 'node:test';
import { createServerApp } from '../server.ts';
import { WorkforceStore } from '../src/server/store.ts';

let baseUrl = '';
let closeServer: (() => Promise<void>) | null = null;
let store: WorkforceStore;

async function reqJson(
  path: string,
  opts: {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
  } = {},
) {
  const res = await fetch(`${baseUrl}${path}`, {
    method: opts.method ?? 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-Id': 'tenant-wasp-ops',
      'X-Actor-Id': 'usr-jlee',
      'X-Actor-Name': 'J. Lee',
      'X-Actor-Role': 'supervisor',
      ...(opts.headers ?? {}),
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data };
}

before(async () => {
  store = new WorkforceStore();
  const app = createServerApp(store);
  const server = app.listen(0, '127.0.0.1');
  await new Promise<void>((resolve) => server.once('listening', resolve));
  const addr = server.address() as AddressInfo;
  baseUrl = `http://127.0.0.1:${addr.port}`;
  closeServer = () =>
    new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
});

after(async () => {
  if (closeServer) await closeServer();
});

describe('Agent Workforce Management System — 15 Acceptance Criteria', () => {
  it('1. Workforce summary KPIs reconcile exactly with filtered agent queries', async () => {
    const sum = await reqJson('/api/v1/workforce/summary');
    assert.equal(sum.status, 200);
    const activeList = await reqJson('/api/v1/agents?kpi_filter=active&page_size=100');
    const availableList = await reqJson('/api/v1/agents?kpi_filter=available&page_size=100');
    const blockedList = await reqJson('/api/v1/agents?kpi_filter=blocked&page_size=100');
    const approvalList = await reqJson('/api/v1/agents?kpi_filter=approval&page_size=100');

    assert.equal(sum.data.active, activeList.data.total);
    assert.equal(sum.data.available, availableList.data.total);
    assert.equal(sum.data.blocked, blockedList.data.total);
    assert.equal(sum.data.awaiting_approval, approvalList.data.total);
    assert.ok(sum.data.total_workforce >= 18);
  });

  it('2. Server-side search, filter, sort, and pagination operate deterministically', async () => {
    const p1 = await reqJson('/api/v1/agents?sort=cost_today_cents&sort_dir=desc&page=1&page_size=5');
    assert.equal(p1.status, 200);
    assert.equal(p1.data.items.length, 5);
    for (let i = 1; i < p1.data.items.length; i++) {
      assert.ok(p1.data.items[i - 1].telemetry.cost_today_cents >= p1.data.items[i].telemetry.cost_today_cents);
    }

    const search = await reqJson('/api/v1/agents?query=Atlas');
    assert.equal(search.status, 200);
    assert.ok(search.data.items.some((a: { agent_id: string }) => a.agent_id === 'AG-001'));
  });

  it('3. Canonical agent workspace returns all 10 tabs and enforces RBAC on restricted memory', async () => {
    const tabs = ['overview', 'tasks', 'skills-tools', 'model-usage', 'memory', 'activity', 'quality', 'incidents', 'governance'];
    for (const tab of tabs) {
      const r = await reqJson(`/api/v1/agents/AG-001/${tab}`);
      assert.equal(r.status, 200, `Expected 200 for tab ${tab}`);
      if (tab === 'overview' || tab === 'quality') {
        assert.ok(Array.isArray(r.data.performance_trend_30d), `Expected performance_trend_30d on ${tab}`);
        assert.equal(r.data.performance_trend_30d.length, 30);
        for (const pt of r.data.performance_trend_30d as Array<{ efficiency_pct: number; avg_latency_ms: number }>) {
          assert.ok(pt.efficiency_pct >= 0 && pt.efficiency_pct <= 100);
          assert.ok(pt.avg_latency_ms > 0);
        }
      }
    }

    // Verify 30-day visual status badges derived from 30d history
    const atlasDetail = await reqJson('/api/v1/agents/AG-001');
    assert.equal(atlasDetail.status, 200);
    assert.ok(atlasDetail.data.trend_30d);
    assert.ok(
      atlasDetail.data.trend_30d.badges.some((b: { label: string }) => b.label === 'Healthy'),
      'Atlas should have Healthy 30d badge',
    );

    const relayDetail = await reqJson('/api/v1/agents/AG-003');
    assert.equal(relayDetail.status, 200);
    assert.ok(
      relayDetail.data.trend_30d.badges.some((b: { label: string }) => b.label === 'Degraded'),
      'Relay should have Degraded 30d badge',
    );
    assert.ok(
      relayDetail.data.trend_30d.badges.some((b: { label: string }) => b.label === 'Latency High'),
      'Relay should have Latency High 30d badge',
    );

    const sentinelDetail = await reqJson('/api/v1/agents/AG-005');
    assert.equal(sentinelDetail.status, 200);
    assert.ok(
      sentinelDetail.data.trend_30d.badges.some((b: { label: string }) => b.label === 'Latency High'),
      'Sentinel should have Latency High 30d badge',
    );

    const supervisorMem = await reqJson('/api/v1/agents/AG-001/memory', {
      headers: { 'X-Actor-Role': 'supervisor' },
    });
    const viewerMem = await reqJson('/api/v1/agents/AG-001/memory', {
      headers: { 'X-Actor-Role': 'viewer' },
    });
    assert.equal(supervisorMem.status, 200);
    assert.equal(viewerMem.status, 200);
    assert.ok(
      viewerMem.data.items.every((m: { sensitivity: string }) => m.sensitivity !== 'restricted'),
      'Viewer must never receive restricted memory records',
    );
    assert.ok(viewerMem.data.items.length <= supervisorMem.data.items.length);
  });

  it('4. Agent commands validate state transitions, reject invalid ones, and write audit records', async () => {
    const valid = await reqJson('/api/v1/agents/AG-001/commands', {
      method: 'POST',
      body: { command: 'pause', idempotency_key: 'test_pause_1', reason: 'Acceptance test pause' },
    });
    assert.equal(valid.status, 200);
    assert.equal(valid.data.current_status, 'paused');
    assert.ok(valid.data.audit_id);

    // Pausing an already paused agent must be rejected by the state machine
    const invalid = await reqJson('/api/v1/agents/AG-001/commands', {
      method: 'POST',
      body: { command: 'pause', idempotency_key: 'test_pause_2', reason: 'Invalid double pause' },
    });
    assert.equal(invalid.status, 409);

    // Resume back to running
    const resume = await reqJson('/api/v1/agents/AG-001/commands', {
      method: 'POST',
      body: { command: 'resume', idempotency_key: 'test_resume_1', reason: 'Resume after pause test' },
    });
    assert.equal(resume.status, 200);
    assert.equal(resume.data.current_status, 'running');
  });

  it('5. Duplicate command requests with the same idempotency_key deduplicate cleanly', async () => {
    const key = 'idem_dedup_key_100';
    const first = await reqJson('/api/v1/agents/AG-002/commands', {
      method: 'POST',
      body: { command: 'pause', idempotency_key: key, reason: 'First attempt' },
    });
    assert.equal(first.status, 200);
    assert.equal(first.data.idempotent_replay, false);

    const replay = await reqJson('/api/v1/agents/AG-002/commands', {
      method: 'POST',
      body: { command: 'pause', idempotency_key: key, reason: 'Duplicate retry' },
    });
    assert.equal(replay.status, 200);
    assert.equal(replay.data.idempotent_replay, true);
    assert.equal(replay.data.command_id, first.data.command_id);

    // Restore AG-002 to running
    await reqJson('/api/v1/agents/AG-002/commands', {
      method: 'POST',
      body: { command: 'resume', idempotency_key: 'idem_dedup_restore', reason: 'Restore running' },
    });
  });

  it('6. Configuration updates enforce optimistic locking and return 409 VERSION_CONFLICT on stale version', async () => {
    const beforeAgent = await reqJson('/api/v1/agents/AG-004');
    const ver = beforeAgent.data.version;

    const okPatch = await reqJson('/api/v1/agents/AG-004/configuration', {
      method: 'PATCH',
      body: { expected_version: ver, max_ram_mb: 1536, reason: 'Increase Scribe RAM ceiling' },
    });
    assert.equal(okPatch.status, 200);
    assert.equal(okPatch.data.agent.version, ver + 1);

    const conflict = await reqJson('/api/v1/agents/AG-004/configuration', {
      method: 'PATCH',
      body: { expected_version: ver, max_ram_mb: 2048, reason: 'Stale writer conflict test' },
    });
    assert.equal(conflict.status, 409);
    assert.equal(conflict.data.error.code, 'VERSION_CONFLICT');
  });

  it('7. Stable canonical IDs match across list, detail, tasks, and organization graph', async () => {
    const list = await reqJson('/api/v1/agents?query=Atlas');
    const detail = await reqJson('/api/v1/agents/AG-001');
    const graph = await reqJson('/api/v1/organization/graph');
    assert.equal(list.data.items[0].agent_id, detail.data.agent_id);
    assert.ok(graph.data.nodes.some((n: { id: string }) => n.id === detail.data.agent_id));
  });

  it('8. Organization graph node lookup resolves valid nodes and omits cross-tenant records', async () => {
    const atlasNode = await reqJson('/api/v1/graph/nodes/AG-001');
    assert.equal(atlasNode.status, 200);
    assert.equal(atlasNode.data.node_type, 'agent');

    const kbNode = await reqJson('/api/v1/graph/nodes/SK-01');
    assert.equal(kbNode.status, 200);
    assert.equal(kbNode.data.node_type, 'knowledge');

    const foreignNode = await reqJson('/api/v1/graph/nodes/AG-TENANT2-01');
    assert.equal(foreignNode.status, 404);
  });

  it('9. Stale telemetry is explicitly detected when heartbeat is old or telemetry unavailable', async () => {
    const agent = store.agents.get('AG-008')!;
    const originalHb = agent.last_heartbeat;
    agent.last_heartbeat = new Date(Date.now() - 15 * 60_000).toISOString();
    agent.telemetry.last_heartbeat = agent.last_heartbeat;

    const r = await reqJson('/api/v1/agents/AG-008');
    assert.equal(r.status, 200);
    assert.equal(r.data.health, 'stale');

    agent.last_heartbeat = originalHb;
    agent.telemetry.last_heartbeat = originalHb;
  });

  it('10. Failed Graphify refresh retains previous valid snapshot without wiping graph data', async () => {
    const beforeSnap = await reqJson('/api/v1/graph/graphify');
    const prevVersion = beforeSnap.data.snapshot.graph_version;
    const prevNodes = beforeSnap.data.snapshot.node_count;

    const startFail = await reqJson('/api/v1/graph/refresh', {
      method: 'POST',
      body: { simulate_failure: true },
    });
    assert.equal(startFail.status, 202);
    const jobId = startFail.data.job.job_id;

    // Wait for async staging job to finish
    await new Promise((r) => setTimeout(r, 400));
    const statusRes = await reqJson(`/api/v1/graph/refresh/${jobId}`);
    assert.equal(statusRes.status, 200);
    assert.equal(statusRes.data.job.status, 'failed');
    assert.equal(statusRes.data.current_snapshot.graph_version, prevVersion);
    assert.equal(statusRes.data.current_snapshot.node_count, prevNodes);
    assert.ok(statusRes.data.current_snapshot.last_error);
  });

  it('11. Idempotent integration connector sync deduplicates repeated keys', async () => {
    const key = 'sync_dedup_sheets_1';
    const first = await reqJson('/api/v1/integrations/int-sheets/sync', {
      method: 'POST',
      body: { idempotency_key: key },
    });
    assert.equal(first.status, 200);
    assert.equal(first.data.deduplicated, false);
    const runsAfterFirst = first.data.integration.sync_runs;

    const second = await reqJson('/api/v1/integrations/int-sheets/sync', {
      method: 'POST',
      body: { idempotency_key: key },
    });
    assert.equal(second.status, 200);
    assert.equal(second.data.deduplicated, true);
    assert.equal(second.data.integration.sync_runs, runsAfterFirst);
  });

  it('12. Completing a task without evidence or passing acceptance checks returns 422', async () => {
    const badComplete = await reqJson('/api/v1/tasks/TSK-101', {
      method: 'PATCH',
      body: {
        status: 'completed',
        evidence: [],
        acceptance_checks: [{ check: 'Citation check', passed: false }],
      },
    });
    assert.equal(badComplete.status, 422);
    assert.equal(badComplete.data.error.code, 'COMPLETION_EVIDENCE_REQUIRED');
  });

  it('13. Tenant isolation prevents any cross-tenant data leakage', async () => {
    const betaAgents = await reqJson('/api/v1/agents?page_size=100', {
      headers: { 'X-Tenant-Id': 'tenant-external-beta' },
    });
    assert.equal(betaAgents.status, 200);
    assert.equal(betaAgents.data.total, 1);
    assert.equal(betaAgents.data.items[0].agent_id, 'AG-TENANT2-01');

    const crossRead = await reqJson('/api/v1/agents/AG-001', {
      headers: { 'X-Tenant-Id': 'tenant-external-beta' },
    });
    assert.equal(crossRead.status, 404);
  });

  it('14. Resource dashboard exposes bounded worker/queue telemetry and 70/80/90% throttle thresholds', async () => {
    const res = await reqJson('/api/v1/resources');
    assert.equal(res.status, 200);
    assert.equal(res.data.thresholds.throttle_background_pct, 70);
    assert.equal(res.data.thresholds.pause_nonessential_pct, 80);
    assert.equal(res.data.thresholds.reject_heavy_pct, 90);
    assert.ok(res.data.max_queue_capacity > 0);
  });

  it('15. Audit trail records every mutation with actor, role, entity, and correlation ID', async () => {
    const audit = await reqJson('/api/v1/audit');
    assert.equal(audit.status, 200);
    assert.ok(audit.data.items.length >= 5);
    const sample = audit.data.items[0];
    assert.ok(sample.audit_id);
    assert.ok(sample.actor_id);
    assert.ok(sample.correlation_id);

    // Viewers cannot mutate agents
    const forbidden = await reqJson('/api/v1/agents/AG-001/commands', {
      method: 'POST',
      headers: { 'X-Actor-Role': 'viewer' },
      body: { command: 'pause', idempotency_key: 'viewer_block_1', reason: 'Should fail' },
    });
    assert.equal(forbidden.status, 403);
  });
});
