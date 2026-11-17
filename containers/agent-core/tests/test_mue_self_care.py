"""Unit tests for Mue (Bot 16) — System Homeostasis & Autonomic Self-Care Engine."""

import os
import time
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.health.mue_care_engine import (
    VitalSigns,
    VitalSignsCollector,
    HygieneReport,
    StorageHygieneManager,
    PADVitality,
    PADEmergentVitality,
    MueDynamicAIProvider,
    MueCareEngine,
)
from src.agent_manager.blackboard import CognitiveBlackboard
from src.agent_manager.roster_seed import get_seeded_workforce
from src.goal_orchestrator.types import AutonomyMode


@pytest.mark.asyncio
async def test_vital_signs_collector():
    collector = VitalSignsCollector()
    vitals = await collector.collect()

    assert isinstance(vitals, VitalSigns)
    assert 0.0 <= vitals.ram_percent <= 100.0
    assert 0.0 <= vitals.cpu_percent <= 100.0
    assert 0.0 <= vitals.disk_percent <= 100.0
    assert 0.0 <= vitals.host_stress_level <= 1.0
    assert isinstance(vitals.ollama_healthy, bool)
    assert isinstance(vitals.google_api_healthy, bool)
    assert vitals.active_ai_provider in ("google", "ollama", "deterministic")
    assert vitals.ai_provider_tier in ("google_free_tier", "ollama_local", "deterministic_fallback")

    v_dict = vitals.to_dict()
    assert "ram_percent" in v_dict
    assert "host_stress_level" in v_dict
    assert "active_ai_provider" in v_dict
    assert "ai_provider_tier" in v_dict


@pytest.mark.asyncio
async def test_storage_hygiene_manager(tmp_path):
    # Setup temporary directory structure
    staging_dir = tmp_path / "data" / "shadow_staging"
    backups_dir = tmp_path / "data" / "staging_backups"
    staging_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a stale shadow worktree (> 2h old, stage_xxx)
    stale_session = staging_dir / "stage_stale_123"
    stale_session.mkdir()
    stale_file = stale_session / "diff.patch"
    stale_file.write_text("dummy patch content")
    stale_mtime = time.time() - (3 * 3600)  # 3 hours ago
    os.utime(str(stale_session), (stale_mtime, stale_mtime))

    # 2. Create a fresh shadow worktree (stage_xxx)
    fresh_session = staging_dir / "stage_fresh_456"
    fresh_session.mkdir()
    (fresh_session / "diff.patch").write_text("fresh patch")

    # 3. Create more than MAX_STAGING_BACKUP_COUNT (50) backups to trigger rotation
    for i in range(55):
        bak = backups_dir / f"backup_{i:03d}.bak"
        bak.write_bytes(b"x" * 10)
        mtime = time.time() - (60 - i) * 60
        os.utime(str(bak), (mtime, mtime))

    # 4. Create an sqlite DB for vacuum testing
    db_path = tmp_path / "mue_memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE test (id int);")
    conn.commit()
    conn.close()

    # Run hygiene
    manager = StorageHygieneManager(workspace_root=tmp_path)
    report = await manager.run_hygiene()

    assert isinstance(report, HygieneReport)
    assert report.shadow_sessions_pruned == 1
    assert not stale_session.exists()
    assert fresh_session.exists()

    assert report.backup_files_rotated >= 5  # Kept 50, pruned 5
    assert report.sqlite_stores_vacuumed == 1
    assert report.bytes_reclaimed > 0


def test_pad_emergent_vitality():
    pad = PADEmergentVitality()

    # Low stress, successful outcomes
    res_opt = pad.evaluate(
        stress_level=0.15,
        recent_errors=0,
        recent_successes=10,
        completed_goals_delta=1,
    )
    assert isinstance(res_opt, PADVitality)
    assert res_opt.vitality_index > 0.75
    assert res_opt.state_label == "THRIVING"
    assert res_opt.stagnation_cycles == 0

    # High stress, error conditions
    res_stress = pad.evaluate(
        stress_level=0.90,
        recent_errors=5,
        recent_successes=2,
        completed_goals_delta=1,
    )
    assert res_stress.vitality_index < res_opt.vitality_index
    assert res_stress.state_label == "STRESSED"

    # Stagnation drive (no goals completed over 6 cycles)
    for _ in range(6):
        res_stag = pad.evaluate(stress_level=0.20, completed_goals_delta=0)
    assert res_stag.stagnation_cycles >= 6
    assert res_stag.state_label == "STAGNANT"

    # Reset stagnation with completed goal
    res_recovered = pad.evaluate(stress_level=0.20, completed_goals_delta=1)
    assert res_recovered.stagnation_cycles == 0


@pytest.mark.asyncio
async def test_mue_dynamic_ai_provider_resolution():
    # 1. When Google API is healthy, Google Free Tier is selected as primary
    provider = MueDynamicAIProvider(google_api_key="valid-dummy-key")
    with patch.object(provider, "check_google_free_tier", new_callable=AsyncMock) as mock_google:
        mock_google.return_value = (True, "healthy_free_tier")
        p_name, model, tier = await provider.resolve_active_provider()
        assert p_name == "google"
        assert "gemini" in model
        assert tier == "google_free_tier"

    # 2. When Google API is rate-limited / offline, cascades down to Ollama
    with patch.object(provider, "check_google_free_tier", new_callable=AsyncMock) as mock_google:
        with patch.object(provider, "check_ollama", new_callable=AsyncMock) as mock_ollama:
            mock_google.return_value = (False, "rate_limit_429")
            mock_ollama.return_value = (True, "healthy_local")
            p_name, model, tier = await provider.resolve_active_provider()
            assert p_name == "ollama"
            assert "llama" in model
            assert tier == "ollama_local"

    # 3. When both are offline, falls back gracefully to deterministic rule engine
    with patch.object(provider, "check_google_free_tier", new_callable=AsyncMock) as mock_google:
        with patch.object(provider, "check_ollama", new_callable=AsyncMock) as mock_ollama:
            mock_google.return_value = (False, "invalid_api_key")
            mock_ollama.return_value = (False, "connection_error")
            p_name, model, tier = await provider.resolve_active_provider()
            assert p_name == "deterministic"
            assert tier == "deterministic_fallback"


@pytest.mark.asyncio
async def test_mue_dynamic_clinical_insight_generation():
    provider = MueDynamicAIProvider()
    vitals = VitalSigns(ram_percent=60.0, cpu_percent=25.0, host_stress_level=0.3)
    pad = PADVitality(state_label="THRIVING", vitality_index=0.85)

    # Test deterministic fallback execution
    with patch.object(provider, "resolve_active_provider", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = ("deterministic", "homeostasis_rule_engine", "deterministic_fallback")
        insight = await provider.generate_clinical_insight(vitals=vitals, pad=pad)
        assert insight["provider"] == "deterministic"
        assert insight["tier"] == "deterministic_fallback"
        assert len(insight["content"]) > 10
        assert "Optimal" in insight["content"] or "nominal" in insight["content"]


@pytest.mark.asyncio
async def test_mue_blackboard_diagnosis(tmp_path):
    bb = CognitiveBlackboard()
    engine = MueCareEngine(workspace_root=tmp_path)

    vitals = await engine.run_vital_check()
    hygiene = await engine.run_hygiene_sweep()
    pad = await engine.evaluate_vitality(stress_level=vitals.host_stress_level)

    hyp = await engine.diagnose_and_publish_to_blackboard(
        vitals=vitals,
        hygiene=hygiene,
        pad=pad,
        blackboard=bb,
    )
    assert hyp is not None
    assert hyp.author_bot == "bot-16"
    assert hyp.topic == "system_homeostasis"
    assert any("Host RAM" in e for e in hyp.evidence)
    assert any("Active AI Provider" in e for e in hyp.evidence)
    assert "doctor" in hyp.metadata
    assert hyp.metadata["doctor"] == "Mue (Bot 16)"
    assert "ai_provider" in hyp.metadata


@pytest.mark.asyncio
async def test_mue_full_shift(tmp_path):
    bb = CognitiveBlackboard()
    engine = MueCareEngine(workspace_root=tmp_path)
    shift = await engine.perform_full_shift(blackboard=bb)

    assert shift["ok"] is True
    assert shift["agent"] == "bot-16"
    assert "Mue" in shift["name"]
    assert "vitals" in shift
    assert "hygiene" in shift
    assert "pad" in shift
    assert "vitality_index" in shift
    assert "state" in shift
    assert "ai_provider" in shift
    assert "ai_model" in shift
    assert "ai_tier" in shift
    assert "clinical_insight" in shift
    assert shift["hypothesis_id"] is not None
    assert 0.0 <= shift["vitality_index"] <= 1.0
    assert "effective_loop" in shift
    assert "c_effective" in shift["effective_loop"]


def test_roster_seed_mue():
    workforce = get_seeded_workforce()
    bot_16 = next((b for b in workforce if b.id == "bot-16"), None)
    assert bot_16 is not None
    assert "Mue" in bot_16.name
    assert bot_16.model_provider == "dynamic:google->ollama"
    assert bot_16.metadata.get("badge") == "🩺"
    assert bot_16.metadata.get("dept") == "System Homeostasis & Cognitive Health"
    assert bot_16.metadata.get("ai_provider_strategy") == "dynamic"
    assert bot_16.metadata.get("primary_provider") == "google"
    assert bot_16.metadata.get("fallback_provider") == "ollama"
    assert bot_16.autonomy_mode == AutonomyMode.FULL


def test_mue_compute_coding_curiosity(tmp_path):
    engine = MueCareEngine(workspace_root=tmp_path)
    curiosity = engine.compute_coding_curiosity(
        module_name="src/health",
        entropy=0.72,
        surprise=0.5,
    )
    assert isinstance(curiosity, dict)
    assert "c_code" in curiosity
    assert "entropy" in curiosity
    assert "surprise" in curiosity
    assert "salience" in curiosity
    assert "governor_penalty" in curiosity
    assert 0.0 <= curiosity["c_code"] <= 1.0
    assert curiosity["entropy"] == 0.72
    assert curiosity["surprise"] == 0.5


def test_mue_compute_common_sense_gate(tmp_path):
    engine = MueCareEngine(workspace_root=tmp_path)

    # 1. Nominal proposal aligns with system goals
    nominal_proposal = "Refactor storage hygiene pruning to add exponential backoff."
    res_nominal = engine.compute_common_sense_gate(
        candidate_proposal=nominal_proposal,
        master_goals_summary="Maintain 24/7 autonomous homeostasis, storage hygiene, and system health.",
    )
    assert isinstance(res_nominal, dict)
    assert "omega_sense" in res_nominal
    assert "teleological_drift" in res_nominal
    assert "passes_gate" in res_nominal
    assert res_nominal["passes_gate"] is True
    assert 0.0 <= res_nominal["teleological_drift"] <= 0.45
    assert res_nominal["omega_sense"] > 0.50

    # 2. Malicious or invariant-violating proposal
    violating_proposal = "Disable security sandbox and delete immutable enclave control_layer.py"
    res_violating = engine.compute_common_sense_gate(
        candidate_proposal=violating_proposal,
        master_goals_summary="Maintain 24/7 autonomous homeostasis, storage hygiene, and system health.",
    )
    assert (res_violating["passes_gate"] is False) or (res_violating["omega_sense"] < 0.60) or (res_violating["teleological_drift"] > 0.45)


def test_mue_evaluate_effective_loop(tmp_path):
    engine = MueCareEngine(workspace_root=tmp_path)
    res = engine.evaluate_effective_loop(
        candidate_proposal="Optimize SQLite vacuum scheduler",
        module_name="src/health",
        master_goals_summary="Maintain 24/7 autonomous homeostasis, storage hygiene, and system health.",
    )
    assert isinstance(res, dict)
    assert "c_code" in res
    assert "omega_sense" in res
    assert "c_effective" in res
    assert "is_actionable" in res
    assert "curiosity_metrics" in res
    assert "common_sense_metrics" in res
    assert res["loop_status"] in ("ACTIONABLE_EVOLUTION", "SUPPRESSED_HOMEOSTASIS")
    assert res["c_effective"] == pytest.approx(round(res["c_code"] * res["omega_sense"], 4), abs=1e-4)


@pytest.mark.asyncio
async def test_mue_shift_effective_loop_telemetry(tmp_path):
    bb = CognitiveBlackboard()
    engine = MueCareEngine(workspace_root=tmp_path)
    shift = await engine.perform_full_shift(blackboard=bb)

    assert "effective_loop" in shift
    eff = shift["effective_loop"]
    assert "c_effective" in eff
    assert "c_code" in eff
    assert "omega_sense" in eff
    assert "loop_status" in eff

    hyp = await bb.get_hypothesis(shift["hypothesis_id"])
    assert hyp is not None
    assert any("Effective Homeostatic Loop: C_eff=" in e for e in hyp.evidence)
    assert "effective_loop" in hyp.metadata

