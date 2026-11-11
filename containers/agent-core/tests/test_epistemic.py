from src.agent.epistemic import detect_domains


def test_domain_detection_requires_whole_keyword_tokens():
    assert detect_domains("rapid prototype") == []


def test_domain_detection_matches_keywords_and_phrases():
    assert "programming" in detect_domains("The API failed.")
    assert "data_analysis" in detect_domains("We used machine learning.")
