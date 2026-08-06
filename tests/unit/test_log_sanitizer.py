import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import requests

from blockchain.log_sanitizer import redact_urls, REDACTION_PLACEHOLDER


def test_redacts_api_key_from_requests_http_error():
    """The exact leak vector observed in practice: requests.HTTPError's
    default __str__ appends the full request URL, which for Alchemy
    embeds the API key in its path.
    """
    response = requests.Response()
    response.status_code = 400
    response.url = "https://polygon-mainnet.g.alchemy.com/v2/alch_SECRETKEY123"
    try:
        response.raise_for_status()
    except Exception as e:
        redacted = redact_urls(e)

    assert "alch_SECRETKEY123" not in redacted
    assert "alchemy.com" not in redacted
    assert REDACTION_PLACEHOLDER in redacted
    assert "400 Client Error" in redacted  # diagnostic prefix preserved


def test_redacts_websocket_urls():
    redacted = redact_urls(Exception("connect failed: wss://host.example/v2/SECRET"))
    assert "SECRET" not in redacted
    assert REDACTION_PLACEHOLDER in redacted


def test_redacts_multiple_urls_in_one_message():
    redacted = redact_urls(
        Exception("tried https://a.example/KEY1 then https://b.example/KEY2")
    )
    assert "KEY1" not in redacted
    assert "KEY2" not in redacted
    assert redacted.count(REDACTION_PLACEHOLDER) == 2


def test_preserves_messages_without_urls():
    """Redacting must not cost diagnostic value -- this exact message was
    what identified a real schema-drift bug during live testing.
    """
    message = "column wallet_profiles.score_stale does not exist"
    assert redact_urls(Exception(message)) == message


def test_falls_back_to_type_name_for_empty_message():
    assert redact_urls(ValueError("")) == "ValueError"


def test_accepts_plain_strings():
    assert redact_urls("plain text") == "plain text"
