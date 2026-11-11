import os
import sys
from unittest.mock import patch

from src.utils.media_signing import (
    sign_media_url,
    generate_signed_media_url,
    verify_media_url,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_verify_media_url_missing_params() -> None:
    secret = "mysecret"

    # Missing exp
    valid, reason = verify_media_url("path/to/media.jpg", None, "signature", secret)
    assert valid is False
    assert reason == "missing exp or sig"

    # Missing sig
    valid, reason = verify_media_url("path/to/media.jpg", "1234567890", None, secret)
    assert valid is False
    assert reason == "missing exp or sig"

    # Missing both
    valid, reason = verify_media_url("path/to/media.jpg", None, None, secret)
    assert valid is False
    assert reason == "missing exp or sig"


def test_verify_media_url_invalid_exp() -> None:
    secret = "mysecret"

    # Non-integer exp
    valid, reason = verify_media_url("path/to/media.jpg", "notanint", "signature", secret)
    assert valid is False
    assert reason == "invalid exp"


@patch('src.utils.media_signing.time.time')
def test_verify_media_url_expired(mock_time: object) -> None:
    secret = "mysecret"
    # Note: the mock is an object at runtime but we only care about setting return_value
    getattr(mock_time, "return_value", None)
    setattr(mock_time, "return_value", 1000)

    # exp is strictly less than current time
    valid, reason = verify_media_url("path/to/media.jpg", "999", "signature", secret)
    assert valid is False
    assert reason == "expired"


@patch('src.utils.media_signing.time.time')
def test_verify_media_url_invalid_signature(mock_time: object) -> None:
    secret = "mysecret"
    setattr(mock_time, "return_value", 1000)

    # valid exp, invalid sig
    valid, reason = verify_media_url("path/to/media.jpg", "1005", "wrong_signature", secret)
    assert valid is False
    assert reason == "invalid signature"


@patch('src.utils.media_signing.time.time')
def test_verify_media_url_valid(mock_time: object) -> None:
    secret = "mysecret"
    setattr(mock_time, "return_value", 1000)
    path = "path/to/media.jpg"
    exp = 1005

    sig = sign_media_url(path, exp, secret)

    valid, reason = verify_media_url(path, str(exp), sig, secret)
    assert valid is True
    assert reason == "ok"


@patch('src.utils.media_signing.time.time')
def test_generate_signed_media_url(mock_time: object) -> None:
    secret = "mysecret"
    setattr(mock_time, "return_value", 1000)
    path = "path/to/media.jpg"

    url = generate_signed_media_url(path, secret, ttl_seconds=300)

    # Check that URL is correctly formatted
    assert url.startswith(path)
    assert "?exp=" in url
    assert "&sig=" in url

    # Extract query params and verify
    parts = url.split("?")
    assert len(parts) == 2
    query = parts[1]

    query_parts = query.split("&")
    exp_str = query_parts[0].replace("exp=", "")
    sig = query_parts[1].replace("sig=", "")

    assert exp_str == "1300"  # 1000 + 300

    # Verify the signature is valid
    valid, reason = verify_media_url(path, exp_str, sig, secret)
    assert valid is True
    assert reason == "ok"
