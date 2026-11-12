"""Dispatch Task 02 Completion Email to benjohncarino@gmail.com."""

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

host = os.getenv("SMTP_HOST", "smtp.gmail.com")
port = int(os.getenv("SMTP_PORT", 587))
user = os.getenv("SMTP_USER", "benjohncarino@gmail.com")
pwd = os.getenv("SMTP_PASSWORD", "")
from_addr = os.getenv("SMTP_FROM", user)
to_addr = user

subject = "[Agent WASP ATSE] Task 02 Completed: Bounded Subagent Genesis System (Depth-1 Spawner)"

body_html = """<html>
<head>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 24px; }
    .card { background-color: #1e293b; border-radius: 12px; padding: 24px; border: 1px solid #334155; max-width: 720px; margin: 0 auto; }
    h2 { color: #38bdf8; margin-top: 0; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 700; background-color: #059669; color: #ffffff; }
    .metric-table { width: 100%; border-collapse: collapse; margin: 16px 0; }
    .metric-table td, .metric-table th { padding: 10px 14px; border: 1px solid #334155; text-align: left; }
    .metric-table th { background-color: #0f172a; color: #94a3b8; }
    .code { font-family: monospace; background-color: #0f172a; padding: 2px 6px; border-radius: 4px; color: #f59e0b; }
    .guardrail { background-color: #0f172a; border-left: 4px solid #38bdf8; padding: 12px; margin: 12px 0; font-family: monospace; font-size: 13px; color: #cbd5e1; }
  </style>
</head>
<body>
  <div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <h2>Autonomous Teleological Self-Evolution (ATSE)</h2>
      <span class="badge">TASK 02 / 06 COMPLETE</span>
    </div>
    <p><b>Executive Briefing:</b> Task 02 (Bounded Subagent Genesis System & Depth-1 Spawner) has been implemented, compiled, and verified across Prometheus (<span class="code">root-liaison</span>) and Athena (<span class="code">bot-15</span>).</p>
    
    <h3>1. Architectural Guardrails Implemented</h3>
    <table class="metric-table">
      <tr><th>Guardrail</th><th>Policy</th><th>Enforcement Mechanism</th></tr>
      <tr>
        <td><b>Strict Depth-1 Limit</b></td>
        <td>Root agents (depth=0) can spawn; Subagents (depth=1) cannot.</td>
        <td><span class="code">DepthLimitExceededError</span> raised if child attempts to spawn. Zero recursive runaway risk.</td>
      </tr>
      <tr>
        <td><b>Concurrency Leash</b></td>
        <td>Max concurrent active subagents capped (default: 3).</td>
        <td><span class="code">ConcurrencyLimitExceededError</span> prevents unbounded task sprawl.</td>
      </tr>
      <tr>
        <td><b>PC-Hardware Fit</b></td>
        <td>Local model requests strictly observe host capacity (CPU-only, 17.4 GB RAM).</td>
        <td>Oversized local models (e.g. 70B/32B/14B) auto-downgraded to <span class="code">llama3.2:latest</span>.</td>
      </tr>
      <tr>
        <td><b>Ephemeral Lifecycle</b></td>
        <td>Temporary tasks report back and are archived upon completion.</td>
        <td>Automatic reaping via <span class="code">reap_subagent()</span>; zero dangling zombie processes.</td>
      </tr>
    </table>

    <h3>2. Verification Results</h3>
    <table class="metric-table">
      <tr><th>Verification Scenario</th><th>Observed Behavior</th><th>Status</th></tr>
      <tr>
        <td><b>Prometheus Genesis</b></td>
        <td>Spawns &lsquo;AST Route Auditor&rsquo; with depth=1, ephemeral=True, budget=1500 tokens.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED</span></td>
      </tr>
      <tr>
        <td><b>Athena Genesis</b></td>
        <td>Spawns &lsquo;Vulnerability Scanner&rsquo; with security capabilities.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED</span></td>
      </tr>
      <tr>
        <td><b>Recursive Spawn Block</b></td>
        <td>Subagent attempting to spawn a child was rejected with DepthLimitExceededError.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED (Blocked)</span></td>
      </tr>
      <tr>
        <td><b>Concurrency Capping</b></td>
        <td>3rd spawn blocked when limit was 2; slot successfully freed after reaping.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED (Capped)</span></td>
      </tr>
      <tr>
        <td><b>Hardware Sizing Guard</b></td>
        <td>Requested <span class="code">deepseek-r1:70b</span> &rarr; Downgraded safely to <span class="code">llama3.2:latest</span>.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED (Fitted)</span></td>
      </tr>
      <tr>
        <td><b>Clean Reaping</b></td>
        <td>All child subagents cleanly archived; parent tracking lists pruned to 0.</td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED (Zero Leak)</span></td>
      </tr>
    </table>

    <h3>3. Next Execution Item</h3>
    <p>As per the <b>"1 task at a time"</b> directive, standing by for user confirmation to execute <b>Task 03: Triad Adversarial Consensus Protocol</b> (&ge; 2/3 majority consensus with Prometheus veto for code changes).</p>
    
    <hr style="border: 0; border-top: 1px solid #334155; margin: 20px 0;">
    <p style="font-size: 12px; color: #64748b; margin-bottom: 0;">Agent WASP Autonomic Mesh &bull; Prometheus Root Liaison &bull; Host: 192.168.1.3</p>
  </div>
</body>
</html>
"""

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = from_addr
msg["To"] = to_addr
msg.attach(MIMEText(body_html, "html"))

print(f"Connecting to SMTP {host}:{port} as {user}...")
try:
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(user, pwd)
        server.send_message(msg)
    print(f"[SUCCESS] Dispatched Task 02 Completion Email to {to_addr}")
except Exception as e:
    print(f"[ERROR] Failed to dispatch email: {e}")
