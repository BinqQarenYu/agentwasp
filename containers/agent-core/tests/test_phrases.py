"""Tests for communication.phrases — canonical English templates."""
from __future__ import annotations

import pytest

from src.communication.phrases import _CATALOGUE, pick


def test_every_intent_has_at_least_one_variant():
    for intent, variants in _CATALOGUE.items():
        assert variants, f"{intent}: empty variant list"
        for v in variants:
            assert isinstance(v, str) and v.strip(), f"{intent}: empty variant"


def test_pick_returns_string_for_known_intent():
    msg = pick("generic_failure", seed="a")
    assert isinstance(msg, str)
    assert msg.strip()


def test_pick_returns_marker_for_unknown_intent():
    msg = pick("nonexistent_intent_zzz")
    assert "missing intent" in msg.lower()


def test_pick_is_deterministic_for_same_seed():
    a = pick("url_required", seed="abc")
    b = pick("url_required", seed="abc")
    assert a == b


def test_pick_varies_across_seeds_when_multiple_variants():
    # generic_failure has 3 variants — at least 2 different seeds should
    # land on different variants (collision risk is tiny with blake2s).
    seen = {pick("generic_failure", seed=str(i)) for i in range(20)}
    assert len(seen) >= 2, "rotation seems stuck on a single variant"


def test_pick_fills_placeholders():
    msg = pick(
        "attribute_truth_single",
        seed="x",
        label="cat's name",
        truth="Pixel",
    )
    assert "Pixel" in msg
    assert "cat's name" in msg


def test_pick_returns_template_when_placeholder_missing():
    # Should not crash if caller forgot a kwarg.
    msg = pick("attribute_truth_single", seed="x", truth="Pixel")
    assert isinstance(msg, str)


def test_no_spanish_in_canonical_catalogue():
    """The whole point of the refactor: catalogue is English-only."""
    spanish_markers = [
        "querés", "pasame", "mandame", "probá", "volvé",
        "decímelo", "confirmame", "tenés", "podés", " vos ",
        "¿Cuál es correcto?", "¿De qué sitio quieres",
    ]
    for intent, variants in _CATALOGUE.items():
        for v in variants:
            lower = v.lower()
            for marker in spanish_markers:
                assert marker.lower() not in lower, (
                    f"{intent} variant contains Spanish marker {marker!r}: {v!r}"
                )


def test_pick_index_distribution():
    """Sanity check that the seed hash actually distributes."""
    from src.communication.phrases import _pick_index
    counts = [0, 0, 0]
    for i in range(300):
        counts[_pick_index(f"seed-{i}", 3)] += 1
    # No bucket should be empty after 300 trials.
    assert all(c > 0 for c in counts), f"distribution stuck: {counts}"
