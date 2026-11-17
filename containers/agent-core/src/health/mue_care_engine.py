"""MueCareEngine — System Homeostasis & Autonomic Self-Care Engine for Agent WASP.

Adapted and evolved from MUE-X (Korro Research / BinqQarenYu) for Agent WASP.
Maintains 24/7 biological preservation, host resource guardrails, cognitive de-cluttering,
and PAD emotional vitality.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import psutil
import structlog

logger = structlog.get_logger()

# Constants
MAX_STAGING_BACKUP_MB = 500
MAX_STAGING_BACKUP_COUNT = 50
STALE_STAGING_MAX_AGE_SEC = 7200  # 2 hours
MAX_HOST_RAM_PERCENT = 85.0       # Threshold for host stress


@dataclass
class VitalSigns:
    """Snapshot of host and container vitality."""
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 17.4
    disk_percent: float = 0.0
    disk_free_gb: float = 0.0
    ollama_healthy: bool = True
    host_stress_level: float = 0.0  # 0.0 (calm) to 1.0 (critical)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HygieneReport:
    """Metrics from an automated storage and memory hygiene sweep."""
    shadow_sessions_pruned: int = 0
    backup_files_rotated: int = 0
    bytes_reclaimed: int = 0
    sqlite_stores_vacuumed: int = 0
    redis_keys_pruned: int = 0
    duration_ms: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PADVitality:
    """Pleasure-Arousal-Dominance (PAD) emotional homeostasis state."""
    pleasure: float = 0.85    # High ratio of task successes vs errors
    arousal: float = 0.25     # System stress / task pressure (lower is calmer)
    dominance: float = 0.80   # Execution agency & stability
    vitality_index: float = 0.84
    stagnation_cycles: int = 0
    state_label: str = "THRIVING"  # THRIVING | CALM | STRESSED | STAGNANT | FATIGUED
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VitalSignsCollector:
    """Collects non-blocking system metrics and service health."""

    def __init__(self, ollama_host: str = "http://localhost:11434"):
        self.ollama_host = ollama_host

    async def collect(self) -> VitalSigns:
        """Asynchronously sample host psutil metrics and service health."""
        # Non-blocking psutil reads in thread pool
        vitals_dict = await asyncio.to_thread(self._sample_psutil)

        # Quick Ollama ping
        ollama_ok = await self._check_ollama()
        vitals_dict["ollama_healthy"] = ollama_ok

        # Compute stress level
        cpu_p = vitals_dict["cpu_percent"]
        ram_p = vitals_dict["ram_percent"]
        stress = min(1.0, (max(cpu_p - 20.0, 0.0) / 80.0) * 0.4 + (ram_p / 100.0) * 0.6)
        vitals_dict["host_stress_level"] = round(stress, 3)

        return VitalSigns(**vitals_dict)

    def _sample_psutil(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        try:
            disk = psutil.disk_usage(os.getcwd())
            disk_free = round(disk.free / (1024**3), 2)
            disk_p = disk.percent
        except Exception:
            disk_free = 50.0
            disk_p = 50.0

        return {
            "cpu_percent": float(cpu),
            "ram_percent": float(mem.percent),
            "ram_used_gb": round(mem.used / (1024**3), 2),
            "ram_total_gb": round(mem.total / (1024**3), 2),
            "disk_percent": float(disk_p),
            "disk_free_gb": float(disk_free),
        }

    async def _check_ollama(self) -> bool:
        try:
            import urllib.request
            def _ping():
                req = urllib.request.Request(f"{self.ollama_host}/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    return resp.status == 200
            return await asyncio.to_thread(_ping)
        except Exception:
            return False


class StorageHygieneManager:
    """Safely prunes transient caches, shadow worktrees, and rotates backups."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.staging_root = self.workspace_root / "data" / "shadow_staging"
        self.backup_root = self.workspace_root / "data" / "staging_backups"

    async def run_hygiene(self) -> HygieneReport:
        t0 = time.monotonic()
        bytes_reclaimed = 0
        sessions_pruned = 0
        backups_rotated = 0
        stores_vacuumed = 0

        # 1. Prune stale shadow staging sessions (> 2h old)
        if self.staging_root.exists():
            now = time.time()
            for session_dir in self.staging_root.iterdir():
                if session_dir.is_dir() and session_dir.name.startswith("stage_"):
                    try:
                        mtime = session_dir.stat().st_mtime
                        if now - mtime > STALE_STAGING_MAX_AGE_SEC:
                            size = sum(f.stat().st_size for f in session_dir.rglob("*") if f.is_file())
                            shutil.rmtree(session_dir, ignore_errors=True)
                            bytes_reclaimed += size
                            sessions_pruned += 1
                    except Exception as exc:
                        logger.warning("mue.hygiene.staging_prune_error", path=str(session_dir), error=str(exc))

        # 2. Rotate staging backups if exceeding budget (500MB)
        if self.backup_root.exists():
            bak_files = sorted(
                [f for f in self.backup_root.iterdir() if f.is_file() and f.name.endswith(".bak")],
                key=lambda x: x.stat().st_mtime,
                reverse=True  # newest first
            )
            total_size = sum(f.stat().st_size for f in bak_files)
            max_bytes = MAX_STAGING_BACKUP_MB * 1024 * 1024

            if total_size > max_bytes or len(bak_files) > MAX_STAGING_BACKUP_COUNT:
                # Keep the newest MAX_STAGING_BACKUP_COUNT
                for old_bak in bak_files[MAX_STAGING_BACKUP_COUNT:]:
                    try:
                        size = old_bak.stat().st_size
                        old_bak.unlink(missing_ok=True)
                        bytes_reclaimed += size
                        backups_rotated += 1
                    except Exception as exc:
                        logger.warning("mue.hygiene.backup_rotate_error", file=str(old_bak), error=str(exc))

        # 3. Optimize local SQLite memory databases if present
        sqlite_candidates = [
            self.workspace_root / "mue_memory.db",
            self.workspace_root / "data" / "agent_memory.db",
        ]
        for db_file in sqlite_candidates:
            if db_file.exists() and db_file.is_file():
                try:
                    await asyncio.to_thread(self._vacuum_db, db_file)
                    stores_vacuumed += 1
                except Exception as exc:
                    logger.warning("mue.hygiene.vacuum_error", db=str(db_file), error=str(exc))

        duration_ms = round((time.monotonic() - t0) * 1000)
        return HygieneReport(
            shadow_sessions_pruned=sessions_pruned,
            backup_files_rotated=backups_rotated,
            bytes_reclaimed=bytes_reclaimed,
            sqlite_stores_vacuumed=stores_vacuumed,
            duration_ms=duration_ms,
        )

    def _vacuum_db(self, path: Path):
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA optimize;")
        finally:
            conn.close()


class PADEmergentVitality:
    """Maintains Pleasure-Arousal-Dominance emotional homeostasis and stagnation pressure."""

    def __init__(self):
        self.stagnation_cycles = 0

    def evaluate(
        self,
        stress_level: float,
        recent_errors: int = 0,
        recent_successes: int = 10,
        completed_goals_delta: int = 1,
    ) -> PADVitality:
        # 1. Pleasure: success vs error balance
        total_outcomes = max(recent_successes + recent_errors, 1)
        pleasure = max(0.1, min(1.0, recent_successes / total_outcomes))

        # 2. Arousal: correlated with system stress
        arousal = max(0.1, min(1.0, 0.2 + 0.8 * stress_level))

        # 3. Dominance: agency stability (decreased under extreme arousal)
        dominance = max(0.2, min(1.0, 0.9 - 0.5 * stress_level))

        # Vitality Index
        vitality = round(pleasure * 0.4 + (1.0 - arousal) * 0.3 + dominance * 0.3, 3)

        # Stagnation tracking (Drive 3 from MUE-X)
        if completed_goals_delta == 0:
            self.stagnation_cycles += 1
        else:
            self.stagnation_cycles = 0

        # State label
        if self.stagnation_cycles >= 6:
            state = "STAGNANT"
        elif stress_level > 0.80:
            state = "STRESSED"
        elif vitality > 0.75:
            state = "THRIVING"
        else:
            state = "CALM"

        return PADVitality(
            pleasure=round(pleasure, 3),
            arousal=round(arousal, 3),
            dominance=round(dominance, 3),
            vitality_index=vitality,
            stagnation_cycles=self.stagnation_cycles,
            state_label=state,
        )


class MueCareEngine:
    """Master orchestrator for Bot 16: Mue (System Homeostasis & Autonomic Self-Care)."""

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        redis_url: str = "",
        ollama_host: str = "http://localhost:11434",
    ):
        if workspace_root:
            self.workspace_root = Path(workspace_root)
        else:
            curr = Path(__file__).resolve()
            found = None
            for p in curr.parents:
                if (p / ".git").exists() or (p / "MEMORY.md").exists() or (p / "run_dashboard_native.py").exists():
                    found = p
                    break
            self.workspace_root = found or curr.parents[4]

        self.redis_url = redis_url
        self.vitals_collector = VitalSignsCollector(ollama_host=ollama_host)
        self.hygiene_manager = StorageHygieneManager(workspace_root=self.workspace_root)
        self.pad_vitality = PADEmergentVitality()

    async def run_vital_check(self) -> VitalSigns:
        return await self.vitals_collector.collect()

    async def run_hygiene_sweep(self) -> HygieneReport:
        return await self.hygiene_manager.run_hygiene()

    async def evaluate_vitality(self, stress_level: float, completed_goals: int = 1) -> PADVitality:
        return self.pad_vitality.evaluate(
            stress_level=stress_level,
            completed_goals_delta=completed_goals
        )

    async def diagnose_and_publish_to_blackboard(
        self,
        vitals: VitalSigns,
        hygiene: HygieneReport,
        pad: PADVitality,
        blackboard=None,
    ) -> Optional[Any]:
        """Publishes an authoritative health and homeostasis diagnosis to Cognitive Blackboard."""
        statement = (
            f"Mue Homeostasis Diagnosis: System is {pad.state_label} (Vitality: {pad.vitality_index:.2f}). "
            f"RAM: {vitals.ram_percent:.1f}% ({vitals.ram_used_gb}GB / {vitals.ram_total_gb}GB), "
            f"CPU: {vitals.cpu_percent:.1f}%, Reclaimed: {round(hygiene.bytes_reclaimed / (1024**2), 2)}MB disk."
        )

        evidence = [
            f"Host RAM: {vitals.ram_percent}% (Limit: 85%)",
            f"Host Stress Index: {vitals.host_stress_level:.3f}",
            f"Ollama Health: {'ONLINE' if vitals.ollama_healthy else 'OFFLINE/UNREACHABLE'}",
            f"Storage Hygiene: {hygiene.shadow_sessions_pruned} shadow sessions pruned, {hygiene.backup_files_rotated} backups rotated",
            f"PAD Emotional State: Pleasure={pad.pleasure}, Arousal={pad.arousal}, Dominance={pad.dominance}",
        ]

        if blackboard is None:
            try:
                from ..agent_manager.blackboard import CognitiveBlackboard
                blackboard = CognitiveBlackboard(redis_url=self.redis_url)
            except Exception:
                return None

        try:
            hyp = await blackboard.post_hypothesis(
                author_bot="bot-16",
                topic="system_homeostasis",
                statement=statement,
                evidence=evidence,
                confidence=round(pad.vitality_index, 2),
                metadata={
                    "vitals": vitals.to_dict(),
                    "hygiene": hygiene.to_dict(),
                    "pad": pad.to_dict(),
                    "doctor": "Mue (Bot 16)",
                }
            )
            logger.info("mue.blackboard_diagnosis_published", hypothesis_id=hyp.id, vitality=pad.vitality_index)
            return hyp
        except Exception as exc:
            logger.warning("mue.blackboard_publish_failed", error=str(exc))
            return None

    async def perform_full_shift(self, blackboard=None) -> Dict[str, Any]:
        """Executes a complete self-care shift."""
        t0 = time.monotonic()
        logger.info("mue.shift_started")

        # 1. Sample vitals
        vitals = await self.run_vital_check()

        # 2. Run hygiene sweep
        hygiene = await self.run_hygiene_sweep()

        # 3. Evaluate emotional vitality
        pad = await self.evaluate_vitality(stress_level=vitals.host_stress_level)

        # 4. Publish to Cognitive Blackboard
        hyp = await self.diagnose_and_publish_to_blackboard(
            vitals=vitals,
            hygiene=hygiene,
            pad=pad,
            blackboard=blackboard,
        )

        duration_ms = round((time.monotonic() - t0) * 1000)
        logger.info(
            "mue.shift_completed",
            vitality=pad.vitality_index,
            state=pad.state_label,
            reclaimed_bytes=hygiene.bytes_reclaimed,
            duration_ms=duration_ms,
        )

        return {
            "ok": True,
            "agent": "bot-16",
            "name": "Mue (System Homeostasis Physician)",
            "state": pad.state_label,
            "vitality_index": pad.vitality_index,
            "vitals": vitals.to_dict(),
            "hygiene": hygiene.to_dict(),
            "pad": pad.to_dict(),
            "hypothesis_id": hyp.id if hyp else None,
            "duration_ms": duration_ms,
        }
