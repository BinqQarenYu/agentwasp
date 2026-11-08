# Architectural Specification: Autonomous Employee & Agent Worker Formulation

> **System Designation:** Agent WASP Sovereign Worker Platform  
> **Core Identity:** Autonomous Digital Employee / Autonomous Knowledge Worker  
> **Mission:** End-to-end task execution, workflow ownership, proactive operations, and continuous workplace self-improvement.

---

## 1. The Fundamental Re-Centering: Worker vs. Scripted Tool

An algorithmic bot follows a rigid IF-THEN tree (e.g. `IF price > threshold THEN trade`).  
An **Autonomous Employee Agent** does something fundamentally different:
1. **Understands Delegation:** Accepts broad, high-level objectives (*"Own client onboarding this week"*, *"Triage all incoming business inquiries"*, *"Audit our deliverables for errors"*).
2. **Exhibits Employee Curiosity:** Not random fascination with trivia, but professional inquisitiveness:
   - *"Why did that workflow step stall?"*
   - *"Is there an undocumented dependency or missing credential blocking this client deliverable?"*
   - *"Can I automate this repetitive manual step to save 4 hours of company time?"*
3. **Respects Organizational Teleology (The Goal):** Every action is bounded by the company/operator's operational deliverables and policies.

---

## 2. Redefining "Curiosity" in a Corporate & Operational Context

In an autonomous employee, **Curiosity** is redefined as **Professional Initiative and Diagnostic Acumen**:

```
[BAD / UNCONTROLLED CURIOSITY]          [GOOD / EMPLOYEE WORKER CURIOSITY]
"Let me read Wikipedia articles        "A recurring invoice from vendor X
 about 19th century clockmaking."      jumped 40% with no notice. Let me 
                                       pull the contract to check price terms
                                       before notifying the boss."
```

### The 4 Pillars of Worker Curiosity:
1. **Diagnostic Curiosity (Root-Cause Investigation):** When a workflow or integration fails, the agent does not merely log `"error code 500"`. It inspects the payload, checks API deprecation notices, tests a dry-run in sandbox, and reports: *"Vendor X changed their auth header format; here is the updated adapter ready for your review."*
2. **Proactive Optimization (Process Improvement):** Noticing repetitive operator requests and automatically drafting standard operating procedures (SOPs) or reusable automation scripts.
3. **Anticipatory Preparedness:** If a meeting or milestone is approaching, curiosity prompts the worker to gather background materials, review historical email correspondence, and assemble a 1-page briefing dossier.
4. **Discrepancy Auditing:** Detecting anomalies in spreadsheets, deliverables, invoices, or customer tickets before they escalate.

---

## 3. Redefining "Goal" as Organizational Alignment & Chain of Command

In an employee agent, the **Goal Vector ($\vec{G}$)** is the **Operational Mandate**:

| Goal Dimension | Autonomous Worker Implementation |
| :--- | :--- |
| **Mission Mandate** | The primary business outcomes delegated by the operator (e.g. zero missed emails, 100% accurate weekly reports). |
| **Authority Boundary** | Clear delineation of what requires human sign-off (spending money, sending unverified public emails, deleting databases) vs. what runs autonomously (research, drafting, triaging, sandbox testing). |
| **Deliverable Done-Definition** | Strict acceptance criteria. A task is not "done" when text is generated; it is done when verified against the user's checklist. |

---

## 4. Re-Calibrated Worker Domain Salience Matrix

The curiosity salience weights in `src/curiosity/evaluator.py` are now explicitly mapped to knowledge worker competencies:

```python
DEFAULT_DOMAIN_SALIENCE = {
    "task_execution": 1.0,          # Doing work delegated by operator
    "workflow_automation": 1.0,     # Optimizing standard operating procedures (SOPs)
    "operations_management": 0.95,  # Tracking deliverables, pipelines, schedules
    "communication_inbox": 0.95,    # Triaging client/team communications, emails
    "quality_assurance": 0.95,      # Auditing work accuracy, catching errors before operator sees them
    "tool_competence": 0.90,        # Mastering APIs, browser scripts, local environment
    "system_integrity": 0.90,       # Health of worker infrastructure, disk, memory
    "documentation": 0.85,          # Keeping clean manuals, handovers, records
    "general_unaligned": 0.15,      # Ambient trivia / unassigned hobbies
}
```

---

## 5. The Employee Autonomous Cycle: The Natural Loop

```
1. RECEIVE MANDATE       Operator defines business objectives & authority bounds.
       |
       v
2. DIAGNOSTIC SURVEY     Worker senses environment (inbox, tasks, calendar, repositories).
       |
       v
3. INITIATIVE FILTER     Worker curiosity identifies friction, gaps, or pending deliverables.
       |                 Applies Teleological Filter: Does this advance the delegated mandate?
       v
4. SAFE EXECUTION        Self-executes low-risk tasks autonomously.
       |                 Suspends high-impact actions for 1-click operator authorization.
       v
5. BRIEFING & HANDOVER   Delivers concise, verified output + updates organizational memory.
```
