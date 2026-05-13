"""Pure-logic coverage for the four 'Experimental' cognitive subsystems.

These tests avoid hitting Postgres / Redis: they exercise the pieces that
are pure-Python (detection regexes, formatting helpers, conflict checks)
so the modules don't drift silently when they have no production data
to validate against.
"""

import pytest

from src.memory import learning, procedural, behavioral


# ──────────────────────────────────────────────────────────────────────
# learning examples
# ──────────────────────────────────────────────────────────────────────
class TestLearningFeedbackDetection:
    @pytest.mark.parametrize("text", [
        "perfecto, gracias",
        "exacto eso era",
        "excelente trabajo",
        "muy bien hecho",
        "thanks, that's great",
        "perfect",
    ])
    def test_positive_feedback(self, text):
        assert learning.detect_feedback(text) == "positive"

    @pytest.mark.parametrize("text", [
        "eso está mal",
        "no es lo que pedí",
        "está incorrecto",
        "wrong, try again",
        "that's not what I wanted",
        "fallaste",
        "intenta de nuevo",
    ])
    def test_negative_feedback(self, text):
        assert learning.detect_feedback(text) == "negative"

    @pytest.mark.parametrize("text", [
        "crea una tarea para mañana",
        "envía un correo a alice@example.com",
        "buscame info sobre python",
        "",
    ])
    def test_no_feedback(self, text):
        assert learning.detect_feedback(text) is None

    def test_long_text_is_not_feedback(self):
        # >20 words → treated as new instruction, not feedback.
        long_text = " ".join(["word"] * 25)
        assert learning.detect_feedback(long_text) is None


class TestLearningFormatting:
    def test_format_empty_returns_empty_string(self):
        assert learning.format_learned_examples([]) == ""

    def test_format_includes_user_input_and_skill_calls(self):
        out = learning.format_learned_examples([
            {"user_input": "search the web", "skill_calls": "web_search(query='x')", "use_count": 3},
        ])
        assert "search the web" in out
        assert "web_search" in out
        assert "LEARNED FROM YOUR FEEDBACK" in out

    def test_format_multiple_examples(self):
        out = learning.format_learned_examples([
            {"user_input": "u1", "skill_calls": "s1", "use_count": 1},
            {"user_input": "u2", "skill_calls": "s2", "use_count": 2},
        ])
        assert "u1" in out and "u2" in out
        assert "s1" in out and "s2" in out


# ──────────────────────────────────────────────────────────────────────
# procedural memory
# ──────────────────────────────────────────────────────────────────────
class TestProceduralSequenceChecks:
    def test_empty_sequence_has_no_failures(self):
        assert procedural._sequence_had_failures([]) is False

    def test_sequence_with_failure_marker(self):
        # _sequence_had_failures scans `output_summary` for known markers.
        seq = [
            {"skill_name": "shell", "output_summary": "ok"},
            {"skill_name": "shell", "output_summary": "[error] command failed"},
        ]
        assert procedural._sequence_had_failures(seq) is True

    def test_sequence_with_blocked_marker(self):
        seq = [{"skill_name": "browser", "output_summary": "page blocked by cloudflare"}]
        assert procedural._sequence_had_failures(seq) is True

    def test_sequence_all_clean(self):
        seq = [{"skill_name": "shell", "output_summary": "ok, command ran"}] * 3
        assert procedural._sequence_had_failures(seq) is False

    def test_empty_sequence_no_repeats(self):
        assert procedural._has_repeated_skill_calls([]) is False

    def test_distinct_skills_no_repeats(self):
        seq = [
            {"skill_name": "browser"},
            {"skill_name": "gmail"},
            {"skill_name": "shell"},
        ]
        assert procedural._has_repeated_skill_calls(seq) is False

    def test_repeated_skill_detected(self):
        seq = [
            {"skill_name": "shell"},
            {"skill_name": "shell"},
            {"skill_name": "browser"},
        ]
        assert procedural._has_repeated_skill_calls(seq) is True


class TestProceduralFormatting:
    def test_empty_returns_empty(self):
        assert procedural.format_procedures_for_context([]) == ""

    def test_format_includes_procedure_name_and_steps(self):
        out = procedural.format_procedures_for_context([
            {
                "name": "send-screenshot-by-email",
                "description": "Capture a page and email it",
                "steps": ["browser(action='capture', url=...)", "gmail(action='send', to=...)"],
            },
        ])
        assert "send-screenshot-by-email" in out
        assert "browser" in out and "gmail" in out


# ──────────────────────────────────────────────────────────────────────
# behavioral rules
# ──────────────────────────────────────────────────────────────────────
class TestBehavioralConflict:
    """_has_conflict flags semantic contradictions (one side negated, the
    other not, with >35% overlap on the non-negation tokens). Pure
    duplicates have the same negation state → NOT a conflict (dedup is a
    separate concern, handled inline in save_rule)."""

    def test_identical_strings_are_not_a_conflict(self):
        # Same content, same negation state → no conflict (it's a duplicate,
        # which the save_rule overlap check handles separately).
        assert behavioral._has_conflict(
            "always answer in english",
            "always answer in english",
        ) is False

    def test_disjoint_strings_dont_conflict(self):
        assert behavioral._has_conflict(
            "schedule jobs on sunday",
            "use markdown for code blocks",
        ) is False

    def test_opposite_negation_with_overlap_conflicts(self):
        # Same topic, opposite negation → real conflict.
        assert behavioral._has_conflict(
            "always answer in english",
            "never answer in english",
        ) is True


class TestBehavioralFormatting:
    def test_format_empty(self):
        assert behavioral.format_for_context([]) == ""

    def test_format_with_rules(self):
        rules = [{
            "id": "r1",
            "rule_type": "refusal",
            "description": "do not refuse capability questions",
            "applied_count": 2,
            "confidence": 0.8,
        }]
        out = behavioral.format_for_context(rules)
        assert "do not refuse capability questions" in out

    def test_extract_fewshots_empty(self):
        assert behavioral.extract_fewshots([]) == []

    def test_extract_poison_patterns_empty(self):
        assert behavioral.extract_poison_patterns([]) == []


# ──────────────────────────────────────────────────────────────────────
# dream cycle activation logic
# ──────────────────────────────────────────────────────────────────────
class TestDreamActivation:
    def test_dream_module_loads(self):
        """Dream-job module imports cleanly — guards against accidental
        breakage of the scheduler wiring."""
        from src.scheduler import dream
        assert hasattr(dream, "DreamJob")
        # The class should be instantiable with the standard kw surface.
        assert callable(dream.DreamJob)
