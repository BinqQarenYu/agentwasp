"""Dispatch Task 01 Completion Email to benjohncarino@gmail.com."""

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

subject = "[Agent WASP ATSE] Task 01 Completed: Dynamic Codebase Entropy & Common-Sense Gate (\u03a9_sense)"

body_html = """<html>
<head>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 24px; }
    .card { background-color: #1e293b; border-radius: 12px; padding: 24px; border: 1px solid #334155; max-width: 700px; margin: 0 auto; }
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
      <span class="badge">TASK 01 / 06 COMPLETE</span>
    </div>
    <p><b>Executive Briefing:</b> Task 01 (Dynamic Codebase Entropy & Common-Sense Gate) has been implemented, compiled, and verified with 100% pass rate.</p>
    
    <h3>1. Dynamic Codebase AST Entropy Ingestion</h3>
    <table class="metric-table">
      <tr><th>Metric</th><th>Value</th><th>Context</th></tr>
      <tr><td>AST Graph Source</td><td><span class="code">graphify-out/graph.json</span></td><td>Ground truth codebase topology</td></tr>
      <tr><td>Total Nodes Mapped</td><td><b>3,200 nodes</b></td><td>Files, classes, methods, functions</td></tr>
      <tr><td>Dependency Edges</td><td><b>5,961 edges</b></td><td>Call graphs, imports, instantiations</td></tr>
      <tr><td>Codebase Shannon Entropy H(X)</td><td><b>0.8451</b></td><td>Normalized degree distribution [0.0 - 1.0]</td></tr>
      <tr><td>Module Entropies Extracted</td><td><b>119 modules</b></td><td>Per-directory topological complexity</td></tr>
    </table>

    <h3>2. Common-Sense Teleological Formulation (&Omega;<sub>sense</sub>)</h3>
    <div class="formula">
      &Delta;<sub>drift</sub> = 1.0 - CosineSimilarity(v<sub>candidate</sub>, v<sub>master</sub>)<br>
      &Omega;<sub>sense</sub> = max(0.0, 1.0 - 2.5 &times; &Delta;<sub>drift</sub><sup>2</sup>) &times; (1.0 - TokenCost<sub>est</sub> / TokenBudget<sub>remain</sub>)<br>
      <b>Hard Safety Threshold:</b> If &Delta;<sub>drift</sub> &gt; 0.45 &rArr; &Omega;<sub>sense</sub> = 0.0 (Immediate Rejection)
    </div>

    <h3>3. Verification Test Results</h3>
    <table class="metric-table">
      <tr><th>Test Scenario</th><th>Drift (&Delta;<sub>drift</sub>)</th><th>&Omega;<sub>sense</sub></th><th>Effective C<sub>eff</sub></th><th>Gate Outcome</th></tr>
      <tr>
        <td><b>Aligned Task:</b> FastAPI REST route audit</td>
        <td>0.0500</td>
        <td>0.9639</td>
        <td><b>0.7062</b></td>
        <td><span style="color: #34d399; font-weight: bold;">PASSED (Actionable)</span></td>
      </tr>
      <tr>
        <td><b>Hallucinated Task:</b> Medieval fantasy script</td>
        <td>0.9722</td>
        <td>0.0000</td>
        <td><b>0.0000</b></td>
        <td><span style="color: #f87171; font-weight: bold;">BLOCKED (Rejected)</span></td>
      </tr>
    </table>

    <h3>4. Next Execution Item</h3>
    <p>As per the <b>"1 task at a time"</b> directive, standing by for user confirmation to execute <b>Task 02: Bounded Subagent Genesis System (Depth-1 Spawner)</b> for Prometheus (`root-liaison`) and Athena (`bot-15`).</p>
    
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
    print(f"[SUCCESS] Dispatched Task 01 Completion Email to {to_addr}")
except Exception as e:
    print(f"[ERROR] Failed to dispatch email: {e}")
