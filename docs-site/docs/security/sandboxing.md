---
id: sandboxing
title: Sandboxing
description: Container isolation, shell/Python sandboxes, SSRF protection, and the cold-start guard.
---

# Sandboxing

WASP executes code, runs browsers, and modifies files. This page documents every sandbox layer and what each one does — and does not — guarantee.

## Container isolation

All WASP services run in Docker containers with non-root users (UID 1000), except `agent-broker` which needs root for Docker socket access.

The agent container itself:

- Non-root user (UID 1000)
- No `--privileged` flag
- Docker CLI talks to `agent-broker`, not the socket directly
- All destructive Docker operations go through the broker's allowlist

## Shell sandbox

The `shell` skill (`skills/builtin/shell.py`, RESTRICTED) executes commands via `asyncio.create_subprocess_exec`:

```python
process = await asyncio.create_subprocess_exec(
    *shlex.split(command),
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd="/data",
)
```

Constraints:

| Setting | Value |
|---------|-------|
| Default timeout | 60 s |
| Max timeout | 120 s |
| Output cap | 8 000 chars |
| Working dir | `/data` |
| User | `agent` (UID 1000) |

Every invocation:

1. Passes the command through `_redact_command()` — strips `sk-*`, `AIza*`, `xai-*`, `hf_*`, and `key=value` / `password=value` patterns.
2. Writes one `AuditLog` row with `action="skill.shell"`, redacted command, exit code, and `goal_id` (when invoked from a goal).

The shell skill has full access to the agent container's filesystem and can run any command the `agent` user can run. **The container itself is the sandbox.** This is defense-in-depth, not a hard sandbox.

## Python sandbox

The `python_exec` skill writes code to a temp file and executes as a subprocess:

```python
with tempfile.NamedTemporaryFile(mode='w', suffix='.py') as f:
    f.write(code)
    tmpfile = f.name

process = await asyncio.create_subprocess_exec(
    sys.executable, tmpfile,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

Constraints: same timeout/output caps as `shell`. Code is parsed with `ast.parse` and the import statements + dangerous-call sites are checked against `_DANGEROUS_IMPORTS`:

```
subprocess, os, sys, pty, ctypes, pickle,
marshal, importlib, __import__, eval, exec, compile
```

The AST scanner blocks code that uses these names directly. A sophisticated prompt injection could potentially bypass this through indirect means; the AST check is defense-in-depth, not a complete sandbox.

## Skill Evolution sandbox

When the Skill Evolution job synthesizes a composite skill, the generated code passes the same AST validation as `python_exec`. Operator approval at `/skill-evolution` is required before activation.

## SSRF protection

SSRF protection is centralized in `src/utils/network_safety.py` (v2.7). Every skill that makes outbound HTTP requests imports `validate_url_for_request()` from this module:

- `http_request`
- `fetch_url`
- `scrape`
- `browser` (URL blocklist at navigation time)
- `monitors`
- `subscriptions` (RSS + price feeds)

Blocked targets:

```
RFC-1918:        10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
Loopback:        127.0.0.0/8, ::1
Link-local:      169.254.0.0/16, fe80::/10
Cloud metadata:  169.254.169.254, fd00:ec2::254,
                 metadata.google.internal, metadata.azure.com
project-blocked: api.telegram.org (avoid leaking the operator's bot token)
```

### DNS rebinding protection

Hostname resolution is performed once, and the **resolved IP** is sent to the HTTP client (via `httpx`'s `transport` config), so an attacker can't return a public IP to pass the check and then resolve to an internal IP on the actual request.

### Manual redirect re-validation

`follow_redirects=True` is disabled. Skills follow redirects manually (max 5 hops), re-validating each `Location:` header against the SSRF guard. This blocks the "redirect to localhost after a public-looking first URL" attack.

19 unit tests cover the SSRF guard (`tests/test_ssrf_guard.py`).

### `browser` URL blocklist

The `browser` skill blocks URLs at navigation time:

| Pattern | Reason |
|---------|--------|
| `file://` | Local file access |
| `javascript:` | XSS vector |
| `data:` | Embedded data exfiltration |
| `vbscript:` | Legacy IE script |
| RFC-1918 ranges, `127.0.0.0/8` | Private network |
| `169.254.169.254`, `fd00:ec2::254` | Cloud metadata IMDS |

### Multi-URL Aggregator: `Error:` prefix detection

When auto-detect resolves multiple URLs in one user message, the multi-URL aggregator builds a per-URL outcome list. The browser skill returns `success=True` even when its output begins with `Error: URL blocked...` (`success` only means the skill itself didn't crash).

The aggregator detects the `Error:` prefix BEFORE checking `[CAPTURE_VALID]` markers — so SSRF-blocked URLs are correctly labeled ❌ with the first-line error, not ✅ navigated.

| Icon | Meaning |
|------|---------|
| ✅ navigated | Successfully reached the URL without screenshot |
| ✅ screenshot sent | Capture taken and attached |
| 🚫 blocked | Login wall or captcha (`[CAPTURE_VALID: false]`) |
| ❌ &lt;error line&gt; | `Error:` prefix detected (SSRF, file://, RFC-1918, etc.) |

## File system access

The `file_ops` skill is constrained to `/data/`. The `self_improve` skill uses `os.path.realpath()` for containment checks:

```python
real_path = os.path.realpath(requested_path)
if not real_path.startswith("/app/src/"):
    raise PermissionError("Path traversal attempt blocked")
```

Symlinks are resolved before the containment check, so `/etc/passwd`-via-symlink attacks fail.

### Self-Improve safety

Before writing any `.py` file via `POST /api/{proposal_id}/apply`:

1. `ast.parse(content)` validates Python syntax. `SyntaxError` returns HTTP 400 with the error truncated to 120 chars; **no file is written**.
2. `shutil.copy2()` creates a timestamped backup at `/data/src_patches/backup_{ts}_{filename}` before overwrite.
3. The success JSON returns `backup_path` so the operator can see the rollback target.

### Self-Improve Soft Safety Gate

`_self_improve_soft_gate()` runs before any write/patch/apply_patch action. Three-tier decision:

| Tier | Condition |
|------|-----------|
| **BLOCK** | Critical path (sandbox.py, control_layer.py, behavioral.py, response_grounder.py, etc.) AND weakening pattern AND (`is_large_patch` OR `is_dense_patch`) |
| **WARN** | Critical path AND (`is_large_patch` OR `is_dense_patch`) without weakening |
| **ALLOW** | Otherwise |

Diff signals: `patch_length > 2000` → `is_large_patch`; `avg_line_length > 120` → `is_dense_patch`.

Weakening patterns (13 of them) include: `disable sandbox`, `bypass guard`, `skip confirmation`, `_HIGH_RISK_ACTIONS=frozenset()`, etc.

All decisions log via `self_improve.soft_gate_analysis` with full metrics.

### SHA-256 sidecar integrity

Every persisted patch in `/data/src_patches/` gets a `.sha256` sidecar. `apply_persisted_patches()` at startup:

- Skips `.sha256` files themselves
- Verifies sidecar if present (mismatch → log warning + skip)
- Applies legacy patches without sidecars with an info log
- Catches all checksum errors fail-open — startup never crashes

## Browser isolation

The browser skill runs Chromium in headless mode:

- No GPU acceleration (headless)
- Profile directories isolated per session name (`/data/browser_sessions/<name>/`)
- Screenshots saved to `/data/screenshots/`
- Cookies persist across calls when using a named session
- Idle Reaper daemon closes Chromium sessions idle > 300 s

## Anticipatory simulation

For RESTRICTED and PRIVILEGED skills, the executor runs a pre-execution simulation. The LLM predicts the outcome and any risks; the result is appended to the skill output for the next round of self-reflection. This is **not** a security control — it's a cognitive self-check that allows the LLM to reconsider an action before execution.

## Cold-Start Hallucination Guard

A short message arriving on a fresh chat with no prior context is one of the most reliable hallucination triggers. The `_is_low_intent()` guard returns True for:

1. **Single ambiguous token** — short confirmations and acknowledgements (multilingual frozenset).
2. **Emoji / digit / punctuation-only** message.
3. **Context-required phrase without anchor** — phrases that explicitly refer to prior interaction (e.g., "do the same", "again", "same as before", and equivalents).
4. **≤ 2 tokens AND every token is in the ambiguous set**.

When low-intent + no scheduled-language match + no `last_exchange` anchor, the handler returns a clarification fast-path in the user's detected language and **never invokes the LLM**.

This is a deterministic guard, not a model behavior — zero token cost, zero hallucination risk on ambiguous cold-start input.

## Secrets handling

Credentials are stored in Redis encrypted via `SecretVault`. The vault key is derived from `DASHBOARD_SECRET`. Never stored in plaintext: integration API keys, Gmail credentials, OAuth tokens.

All audit log entries pass through `redact()` (`utils/redaction.py`) which strips OpenAI `sk-`, Anthropic `sk-ant-`, Google `AIza`, xAI `xai-`, HuggingFace `hf_`, AWS `AKIA`, Stripe `sk_live_`, Slack `xox*`, SendGrid `SG.`, Bearer tokens, and `key=value` password patterns.

## Security best practices

1. Keep `TELEGRAM_ALLOWED_USERS` restricted to your Telegram user IDs.
2. Use a strong `DASHBOARD_SECRET` (≥ 32 random chars).
3. Use a strong `MEDIA_SIGNING_SECRET` (≥ 64 random hex chars).
4. Don't expose port 8080 directly — use the nginx proxy.
5. Regularly review audit logs at `/audit`.
6. Consider `SKILL_EVOLUTION_ENABLED=false` for maximum code safety.
7. Run on a dedicated server — don't share with other services.

## See also

- [Skill Safety](/security/skill-safety) — capability levels, intent gating
- [Privilege Boundaries](/security/privilege-boundaries) — broker, shell, self-improve
- [Audit Logs](/security/audit-logs) — what's logged
- [Testing and Audit](/security/testing-and-audit) — regression suite, audit methodology
