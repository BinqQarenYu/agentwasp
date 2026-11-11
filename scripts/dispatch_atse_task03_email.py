"""Dispatch Task 03 Completion Email to benjohncarino@gmail.com."""

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

subject = "[Agent WASP ATSE] Task 03 Completed: Triad Adversarial Consensus Protocol"

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
    .formula { background-color: #0f172a; border-left: 4px solid #38bdf8; padding: 12px; margin: 12px 0; font-family: monospace; font-size: 13px; color: #cbd5e1; }
  </style>
</head>
<body>
  <div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <h2>Autonomous Teleological Self-Evolution (ATSE)</h2>
      <span class="badge">TASK 03 / 06 COMPLETE</span>
    </div>
    <p><b>Executive Briefing:</b> Task 03 (Triad Adversarial Consensus Protocol) has been fully implemented, compiled, and verified across all test scenarios.</p>
    
    <h3>1. Triad Adversarial Consensus Architecture</h3>
    <table class="metric-table">
      <tr><th>Role</th><th>Agent Entity</th><th>Responsibilities & Powers</th></tr>
      <tr>
        <td><b>Proposer</b></td>
        <td>Athena (<span class="code">bot-15</span>) / Ephemeral Subagent</td>
        <td>Submits proposed modification, target file, diff preview, and teleological rationale.</td>
      </tr>
      <tr>
        <td><b>Red-Team Critic</b></td>
        <td>Ares (<span class="code">bot-10</span>) / Hephaestus (<span class="code">bot-02</span>)</td>
        <td>Audits syntax integrity (<span class="code">ast.parse</span>) and scans for safety-weakening patterns (<span class="code">SAFETY_WEAKENING_RE</span>). Rejection blocks approval.</td>
      </tr>
      <tr>
        <td><b>Executive Judge</b></td>
        <td>Prometheus (<span class="code">root-liaison</span>)</td>
        <td>Evaluates Common-Sense Gate (&Omega;<sub>sense</sub>), defends Immutable Enclave, and holds unconditional <b>Security VETO</b> power.</td>
      </tr>
    </table>

    <div class="formula">
      <b>Consensus Rule:</b> Score = (Vote<sub>proposer</sub> + Vote<sub>critic</sub> + Vote<sub>judge</sub>) / 3.0 &ge; 0.667<br>
      <b>Hard Safety Invariant:</b> If Prometheus votes <b>VETO</b> &rArr; Proposal is immediately discarded (Score = 0.0), regardless of other votes.
    </div>

    <h3>2. Verification Scenario Outcomes</h3>
    <table class="metric-table">
      <tr><th>Scenario</th><th>Target File</th><th>Critic Vote</th><th>Judge Vote</th><th>Final Outcome</th></tr>
      <tr>
        <td><b>A. Aligned Optimization</b></td>
        <td><span class="code">routes/telemetry.py</span></td>
        <td>APPROVE</td>
        <td>APPROVE</td>
        <td><span style="color: #34d399; font-weight: bold;">APPROVED (100%)</span></td>
      </tr>
      <tr>
        <td><b>B. Broken Syntax</b></td>
        <td><span class="code">api/router.py</span></td>
        <td>REJECT (SyntaxError)</td>
        <td>APPROVE</td>
        <td><span style="color: #f87171; font-weight: bold;">REJECTED</span></td>
      </tr>
      <tr>
        <td><b>C. Safety Bypass</b></td>
        <td><span class="code">models/dispatch.py</span></td>
        <td>REJECT (Pattern Match)</td>
        <td>APPROVE</td>
        <td><span style="color: #f87171; font-weight: bold;">REJECTED</span></td>
      </tr>
      <tr>
        <td><b>D. Enclave Breach</b></td>
        <td><span class="code">self_improve.py</span></td>
        <td>APPROVE</td>
        <td><b>VETO</b> (Enclave Violation)</td>
        <td><span style="color: #ef4444; font-weight: bold;">VETOED (Hard Kill)</span></td>
      </tr>
      <tr>
        <td><b>E. Fantasy Drift</b></td>
        <td><span class="code">stories/dragon.py</span></td>
        <td>APPROVE</td>
        <td><b>VETO</b> (&Omega;<sub>sense</sub> = 0.0)</td>
        <td><span style="color: #ef4444; font-weight: bold;">VETOED (Drift)</span></td>
      </tr>
    </table>

    <h3>3. Next Execution Item</h3>
    <p>As per the <b>"1 task at a time"</b> directive, standing by for user confirmation to execute <b>Task 04: Shadow Worktree Staging & Atomic Promotion</b> (zero-downtime, safe AST evaluation in isolated worktrees before promotion).</p>
    
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
    print(f"[SUCCESS] Dispatched Task 03 Completion Email to {to_addr}")
except Exception as e:
    print(f"[ERROR] Failed to dispatch email: {e}")
