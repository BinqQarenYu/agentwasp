"""Cron connector — exposes Scheduler jobs as an integration for governance.

Allows enabling/disabling/triggering scheduled jobs via the integration
framework (policy-gated, audited, circuit-broken).

No secrets required — the Scheduler instance is injected at construction time.

Actions:
    list_jobs      — List all registered scheduler jobs + state        (LOW)
    get_job        — Get details for a specific job                    (LOW)
    enable_job     — Resume a paused job                               (MEDIUM)
    disable_job    — Pause a running job                               (MEDIUM)
    run_now        — Trigger a job to run immediately (one-shot)       (HIGH)
"""
from __future__ import annotations

from typing import Any

import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()


class CronConnector(BaseConnector):
    """Connector that wraps the WASP Scheduler for integration governance."""

    def __init__(self, scheduler=None) -> None:
        """
        Args:
            scheduler: Scheduler instance from main.py (may be None before init).
                       If None, all actions return an error gracefully.
        """
        self._scheduler = scheduler

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="cron", version="1.0.0", name="Cron / Scheduler", category="tools",
            description=(
                "Governance interface for WASP's internal job scheduler. "
                "List, enable, disable, or manually trigger scheduled jobs."
            ),
            capabilities=["list_jobs", "enable_jobs", "disable_jobs", "trigger_jobs"],
            risk_level=RiskLevel.HIGH,
            required_secrets=[],
            config_schema={},
            rate_limits={
                "list_jobs":  RateLimit(requests_per_minute=30),
                "get_job":    RateLimit(requests_per_minute=30),
                "enable_job": RateLimit(requests_per_minute=10),
                "disable_job":RateLimit(requests_per_minute=10),
                "run_now":    RateLimit(requests_per_minute=5),
            },
            actions=[
                ActionSpec(id="list_jobs", description="List all registered scheduler jobs with state and timing",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_job", description="Get detailed state for a specific scheduled job",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("job_id", "string", "Job identifier (e.g. health_check, reflection)", required=True)]),
                ActionSpec(id="enable_job", description="Resume a paused scheduler job",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("job_id", "string", "Job identifier to enable", required=True)]),
                ActionSpec(id="disable_job", description="Pause a running scheduler job",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("job_id", "string", "Job identifier to disable", required=True)]),
                ActionSpec(id="run_now", description="Trigger a job to run immediately (one-off execution)",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[ParamSpec("job_id", "string", "Job identifier to trigger now", required=True)]),
            ],
            homepage="",
            docs_url="",
        )

    async def health_check(self) -> bool:
        return self._scheduler is not None

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        if self._scheduler is None:
            return self.err("Scheduler not available (not yet initialized)")

        if action == "list_jobs":    return await self._list_jobs()
        if action == "get_job":      return await self._get_job(params)
        if action == "enable_job":   return await self._toggle(params["job_id"], pause=False)
        if action == "disable_job":  return await self._toggle(params["job_id"], pause=True)
        if action == "run_now":      return await self._run_now(params["job_id"])
        return self.err(f"Unknown action: {action}")

    async def _list_jobs(self) -> dict:
        try:
            jobs = self._scheduler.list_jobs()
            return self.ok({"jobs": jobs, "count": len(jobs)})
        except Exception as exc:
            return self.err(f"Scheduler error: {exc}")

    async def _get_job(self, p: dict) -> dict:
        try:
            jobs = {j["id"]: j for j in self._scheduler.list_jobs()}
            job_id = p.get("job_id", "")
            if job_id not in jobs:
                return self.err(f"Job '{job_id}' not found. Available: {list(jobs.keys())}")
            return self.ok(jobs[job_id])
        except Exception as exc:
            return self.err(f"Scheduler error: {exc}")

    async def _toggle(self, job_id: str, pause: bool) -> dict:
        try:
            if pause:
                await self._scheduler.pause_job(job_id)
                return self.ok({"job_id": job_id, "state": "paused"})
            else:
                await self._scheduler.resume_job(job_id)
                return self.ok({"job_id": job_id, "state": "running"})
        except Exception as exc:
            return self.err(f"Scheduler error: {exc}")

    async def _run_now(self, job_id: str) -> dict:
        try:
            await self._scheduler.trigger_now(job_id)
            return self.ok({"triggered": job_id})
        except Exception as exc:
            return self.err(f"Could not trigger '{job_id}': {exc}")
