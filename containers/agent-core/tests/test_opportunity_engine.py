from datetime import datetime, timedelta, timezone

from src.opportunity_engine import OpportunityEngine


def _engine():
    return OpportunityEngine(memory=None, model_manager=None, redis_url="", bus=None)


def _event(ts, skills, entities=None):
    return {
        "ts": ts,
        "skills": set(skills),
        "entities": set(entities or []),
    }


def test_web_search_alone_does_not_merge_unrelated_skill_groups():
    start = datetime.now(timezone.utc)
    events = [
        _event(start, {"web_search", "browser"}),
        _event(start + timedelta(hours=4), {"web_search", "gmail"}),
    ]

    assert _engine()._cluster_events(events) == []


def test_shared_non_search_skill_still_merges_related_groups():
    start = datetime.now(timezone.utc)
    events = [
        _event(start, {"web_search", "browser"}),
        _event(start + timedelta(hours=4), {"browser", "gmail"}),
    ]

    assert _engine()._cluster_events(events) == [events]
