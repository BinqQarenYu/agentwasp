import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import express from 'express';
import { createApiRouter } from './src/server/api.ts';
import { WorkforceStore, workforceStore } from './src/server/store.ts';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export function createServerApp(store: WorkforceStore = workforceStore) {
  const app = express();

  app.disable('x-powered-by');
  app.use(express.json({ limit: '1mb' }));

  // Basic security headers compatible with AI Studio preview iframe
  app.use((_req, res, next) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
    next();
  });

  // Serve preserved Agent WASP static icons & brand assets
  const staticDir = path.resolve(__dirname, 'public', 'static');
  if (fs.existsSync(staticDir)) {
    app.use('/static', express.static(staticDir));
  }

  // Health probe
  app.get('/healthz', (_req, res) => {
    res.json({ ok: true, service: 'agent-wasp-workforce', timestamp: new Date().toISOString() });
  });

  // Canonical versioned Workforce Management API
  app.use('/api/v1', createApiRouter(store));

  return app;
}

async function startServer() {
  const app = createServerApp();
  const isProd = process.env.NODE_ENV === 'production';
  const port = Number(process.env.PORT) || 3000;
  const host = '0.0.0.0';

  if (!isProd) {
    const { createServer: createViteServer } = await import('vite');
    const vite = await createViteServer({
      server: { middlewareMode: true, hmr: false },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distDir = path.resolve(__dirname, 'dist');
    app.use(express.static(distDir));
    app.use((_req, res) => {
      res.sendFile(path.join(distDir, 'index.html'));
    });
  }

  // Periodic background heartbeat refresh so running agents stay live without resetting UI state
  const heartbeatTimer = setInterval(() => {
    const now = new Date().toISOString();
    for (const agent of workforceStore.agents.values()) {
      if (agent.status === 'running' && agent.telemetry.telemetry_available) {
        agent.last_heartbeat = now;
        agent.telemetry.last_heartbeat = now;
      }
    }
  }, 30000);
  heartbeatTimer.unref();

  app.listen(port, host, () => {
    console.log(`Agent WASP Workforce Management server listening on http://${host}:${port}`);
  });
}

// Only start HTTP listener when executed directly (not when imported by acceptance tests)
const entryPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (entryPath === __filename) {
  startServer().catch((err) => {
    console.error('Failed to start server:', err);
    process.exit(1);
  });
}
