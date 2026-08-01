"""Client for enriching on-chain trades with resolution data via the Polymarket CLOB API.

Dune's polymarket_polygon.market_trades table (used for the Signal 2 backfill)
has no resolution/settlement column — token_outcome only records which side a
wallet bought, not whether it won. This client fills that gap by looking up
each market directly by condition_id.

Verified live against https://clob.polymarket.com/markets/{condition_id}:
returns 200 with {closed, end_date_iso, tokens: [{outcome, price, winner}]}
for a known market, 404 {"error": "market not found"} otherwise. Gamma API's
condition_ids query param was tried first but does not reliably filter
(confirmed empirically) — the CLOB per-ID endpoint is the reliable source.

Best-effort throughout: any failure (network, 404, ambiguous/no winner)
returns a resolution with both fields None rather than raising. Callers must
treat resolution data as always-optional.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("polymarket.blockchain.market_resolution_client")

CLOB_MARKETS_URL = "https://clob.polymarket.com/markets"
REQUEST_TIMEOUT_SECS = 10


@dataclass
class MarketResolution:
    """Result of a CLOB market resolution lookup."""

    resolved_outcome: Optional[str]
    market_end_time: Optional[float]


class MarketResolutionClient:
    """Looks up a market's resolved outcome and end time by condition_id.

    Caches lookups (including failed ones) in-memory for the lifetime of the
    client instance, since a given condition_id resolves at most once and a
    single backfill run may see the same market many times across wallets.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, MarketResolution] = {}

    def resolve_market(self, condition_id: str) -> MarketResolution:
        """Looks up a market's resolved outcome and end time by condition_id.

        Args:
            condition_id: The market's on-chain condition_id (hex string).

        Returns:
            A MarketResolution with resolved_outcome/market_end_time set to None
            for any field that couldn't be determined (unknown market, network
            failure, market not yet closed, or ambiguous/no-winner outcome).
        """
        if condition_id in self._cache:
            return self._cache[condition_id]

        result = MarketResolution(resolved_outcome=None, market_end_time=None)
        try:
            response = requests.get(
                f"{CLOB_MARKETS_URL}/{condition_id}", timeout=REQUEST_TIMEOUT_SECS
            )
            if response.status_code == 404:
                logger.info(f"No CLOB market found for condition_id={condition_id}")
                self._cache[condition_id] = result
                return result
            response.raise_for_status()
            market = response.json()

            result.market_end_time = _parse_end_time(market.get("end_date_iso"))
            if market.get("closed"):
                for token in market.get("tokens", []):
                    if token.get("winner"):
                        result.resolved_outcome = str(token.get("outcome"))
                        break
        except Exception as e:
            logger.warning(f"CLOB market lookup failed for condition_id={condition_id}: {e}")

        self._cache[condition_id] = result
        return result


def _parse_end_time(end_date_iso: Optional[Any]) -> Optional[float]:
    """Parses an ISO end-date string to a Unix timestamp.

    Catches ValueError/TypeError/AttributeError so a malformed or
    non-string end_date_iso only blanks this field, rather than
    propagating out and suppressing an otherwise-successful outcome lookup.
    """
    if not end_date_iso:
        return None
    try:
        return datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None
