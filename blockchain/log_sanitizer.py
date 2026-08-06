"""Redaction helper for exception text that may embed credentialed URLs.

Exists because RPC endpoints commonly carry an API key in the URL itself
(e.g. Alchemy's https://polygon-mainnet.g.alchemy.com/v2/<KEY>), and
several libraries put the full request URL in an exception's default
string form -- requests.HTTPError renders as
"400 Client Error: Bad Request for url: https://...<KEY>". Logging
`f"{e}"` from any call that touches the network therefore risks writing
a live credential into the logs.

Redacting only the URL (rather than dropping the message and logging
just type(e).__name__) keeps the diagnostically useful part: messages
like "column wallet_profiles.score_stale does not exist" carry the
actual signal, and losing them would make real failures much harder to
debug.

Lives in its own module so both blockchain/polygon_sync.py and
blockchain/event_decoder.py can use it without an import cycle
(polygon_sync already imports event_decoder).
"""

import re

# Matches http(s):// and ws(s):// URLs up to the first whitespace. Trailing
# punctuation is left attached rather than parsed out -- over-redacting a
# closing quote is harmless, under-redacting a key is not.
_URL_PATTERN = re.compile(r"(?:https?|wss?)://\S+", re.IGNORECASE)

REDACTION_PLACEHOLDER = "<redacted-url>"


def redact_urls(text: object) -> str:
    """Returns `text` as a string with any embedded URLs replaced.

    Args:
        text: Typically an Exception; anything str()-able is accepted so
            callers can pass the exception directly.

    Returns:
        The string form with every http(s)/ws(s) URL replaced by
        REDACTION_PLACEHOLDER. Falls back to the exception's type name if
        the result would otherwise be empty, so a log line is never blank.
    """
    rendered = str(text)
    redacted = _URL_PATTERN.sub(REDACTION_PLACEHOLDER, rendered)
    if not redacted.strip():
        return type(text).__name__
    return redacted
