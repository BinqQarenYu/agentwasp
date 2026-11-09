# Agent WASP: System Operations & Integration Guide

## 1. Overview & Architecture
Agent WASP has been upgraded into a sovereign **Autonomous Employee / Knowledge Worker Agent** platform.
- **Port Architecture:**
  - `http://127.0.0.1:1434`: Agent WASP Native Dashboard, Grok/Altair Cockpit, and System Visualizers.
  - `http://127.0.0.1:11434`: Local Ollama LLM Inference Engine.
- **Resource Guardrails:**
  - Hard CPU & Memory limits enforced (`agent-core` $\le$ 3.0 CPUs, $\le$ 4GB RAM) via `docker-compose.override.yml`.
  - Gentle startup orchestrator (`start_gentle.bat`) with a 45s boot cooldown and host RAM health check (<85%).

---

## 2. Grok Bot Mesh Cockpit (Altair "One-Person-Company")
Access at `http://127.0.0.1:1434/mesh` or via sidebar **`Bot Mesh (Altair)`**.

### The 5 Visual Stages:
1. **Inputs:** Circuit nodes for `TASK`, `INBOX` (live Gmail beacon), `CALENDAR`, and `MEMORY.MD`.
2. **Dispatcher:** Minimalist Grok Mask Orb, `Check Gate` (validation rules), and `Splitter Gate` (DAG decomposition).
3. **15 Employee Bots Mesh:**
   - 🟢 `Inbox Triage` (Communication)
   - 🌸 `Meeting Prep` (Operations)
   - 🟣 `Content Calendar` (Marketing)
   - 🔵 `Expense Tracker` (Finance)
   - 🟠 `Bug Repro` (Engineering)
   - 🔴 `Competitor Watch` (Market Intelligence)
   - 🌺 `Travel Planner` (Logistics)
   - 🟣 `Follow-up` (Client Relations)
   - 🟧 `Research` (Epistemic Exploration)
   - 🟩 `Daily Digest` (Executive Briefings)
   - 🔴 `Lead Scout` (Outbound Growth)
   - 🟣 `Invoice Bot` (Billing & Invoicing)
   - 🟠 `Support Desk` (Customer Success)
   - 🟢 `Social Poster` (Distribution)
   - 🔴 `Code Review` (Software Engineering QA)
4. **Micro-Units Execution Waterfall:** Continuous GPU-accelerated fiber-optic stream displaying hundreds of unit badges (`trip.book`, `receipt.scan`, `invoice.send`, `post.draft`, `vat.rate`).
5. **Verified Output Orb & Telemetry:**
   - Big Glowing Green Completion Orb (`409 / 412 verified`).
   - Live telemetry summary card with `inbox · zero` and `posts · 21 queued` sparklines.
   - Human-in-the-loop Payment Approval Gate.
   - Interactive `Pulse Run` button connected to real-time backend SSE (`/mesh/api/trigger-run`).

---

## 3. Google Workspace OAuth Suite Integration
Unified under project credentials configured via environment variables:
- **Project ID:** `aerial-matrix-514209-v8` (configured in `.env` as `GOOGLE_PROJECT_ID`)
- **Client ID:** Configured in `.env` as `GOOGLE_CLIENT_ID`
- **Client Secret:** Configured in `.env` as `GOOGLE_CLIENT_SECRET` (secured in `.env` and `SecretVault`)

### Supported Services:
| Service | Integration ID | Key Actions |
|---|---|---|
| **Google Drive** | `google-drive` | `list_files`, `search_files`, `get_file`, `upload_file`, `create_folder`, `delete_file` |
| **Google Sheets** | `google-sheets` | `read_sheet`, `append_row`, `update_cell`, `create_sheet` |
| **Google Photos** | `google-photos` | `list_media`, `search_media`, `get_photo` |
| **Gmail** | `gmail-connector` | `list_messages`, `read_message`, `send_message`, `search` |
| **Google Calendar** | `google-calendar` | `list_events`, `create_event`, `update_event`, `delete_event` |
| **Google Colab** | *Via Google Drive* | Store, modify, and open `.ipynb` notebooks in Google Drive |

All credentials are saved in `.env` and encrypted within the Redis `SecretVault`.

---

## 4. Host Stability & 24/7 Autonomy
- **Delayed Standalone Startup Shortcut:**
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AgentWASP_Gentle_Startup.lnk` $\to$ executes `start_gentle.bat --boot`.
- **Zero 500 Route Guarantee:**
  Audited with `audit_routes.py` (31 of 31 routes return `200 OK`).
- **Native Server Launcher:**
  `start_dashboard.bat` or `python run_dashboard_native.py` serves the full UI without requiring external Docker daemons.
