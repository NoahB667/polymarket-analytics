"""Pure scoring logic for auto-discovered Polymarket markets.

No API calls, no DB writes, no Redis access -- fully deterministic given a
normalized market dict. See reference/auto_discovery.md (Step 8.5b) for the
full scoring rationale and worked examples this module's tests replicate.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import orjson

# -- Category keyword lists ---------------------------------------------------
SKIP_CATEGORY_KEYWORDS: List[str] = [
    "sports", "football", "basketball", "soccer", "nfl", "nba", "mlb", "nhl",
    "entertainment", "music", "awards", "oscars", "grammy", "celebrity",
    "reality tv", "pop culture", "movies", "gaming",
]

HIGH_VALUE_CATEGORY_KEYWORDS: List[str] = [
    # Geopolitical
    "politics", "election", "geopolitics", "world", "military", "war",
    "ceasefire", "nato", "sanctions", "treaty", "coup", "president",
    "congress", "senate", "parliament",
    # Financial / macro
    "economics", "finance", "fed", "rate", "inflation", "recession", "gdp",
    "tariff", "trade", "cpi", "jobs", "central bank", "interest rate",
    "fiscal",
    # Crypto (medium)
    "bitcoin", "ethereum", "crypto", "sec", "cftc", "regulation", "etf",
    "stablecoin",
    # Corporate
    "merger", "acquisition", "ipo", "earnings", "antitrust",
]

HIGH_VALUE_CATEGORY_BASE_SCORE = 0.4

# -- Default fallback values for malformed inputs ----------------------------
DEFAULT_PRICE = 0.5
DEFAULT_SPREAD = 1.0
SECONDS_PER_DAY = 86400.0

# -- Volume score (higher volume_24h wins the highest bracket it qualifies for) --
VOLUME_BRACKETS: List[Tuple[float, float]] = [
    (500_000.0, 0.30),
    (100_000.0, 0.25),
    (50_000.0, 0.20),
    (10_000.0, 0.10),
    (1_000.0, 0.05),
]

# -- Time-to-resolution score --------------------------------------------------
MIN_DAYS_REMAINING = 2.0
TIME_BRACKETS: List[Tuple[float, float]] = [
    (90.0, 0.20),
    (30.0, 0.15),
    (14.0, 0.10),
    (7.0, 0.05),
]

# -- Liquidity (spread) score -- smaller spread wins the lowest bracket it fits --
SPREAD_BRACKETS: List[Tuple[float, float]] = [
    (0.02, 0.10),
    (0.05, 0.05),
]

# -- Long-shot bonus ------------------------------------------------------------
LONGSHOT_LOW_RANGE = (0.05, 0.20)
LONGSHOT_HIGH_RANGE = (0.80, 0.95)
LONGSHOT_BONUS = 0.05

MAX_SCORE = 1.0


def normalize_market(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extracts the fields score_market needs from a raw Gamma API market dict.

    Args:
        raw: A single market object from a Polymarket Gamma `/events`
            response (expected fields: question, category, volume24hr,
            endDate, closed, bestBid, bestAsk, outcomePrices).

    Returns:
        A normalized dict with keys category, question, volume_24h,
        days_remaining, spread, price, closed -- or None if a required
        field (question or endDate) is missing or unparsable.
    """
    question = raw.get("question")
    end_date_raw = raw.get("endDate")
    if not question or not end_date_raw:
        return None

    try:
        end_date = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    days_remaining = (end_date - now).total_seconds() / SECONDS_PER_DAY

    try:
        volume_24h = float(raw.get("volume24hr", 0.0) or 0.0)
    except (TypeError, ValueError):
        volume_24h = 0.0

    best_bid = raw.get("bestBid")
    best_ask = raw.get("bestAsk")
    try:
        spread = abs(float(best_ask) - float(best_bid)) if best_bid is not None and best_ask is not None else DEFAULT_SPREAD
    except (TypeError, ValueError):
        spread = DEFAULT_SPREAD

    price = DEFAULT_PRICE
    outcome_prices = raw.get("outcomePrices")
    if outcome_prices:
        try:
            if isinstance(outcome_prices, str):
                outcome_prices = orjson.loads(outcome_prices)
            price = float(outcome_prices[0])
        except (TypeError, ValueError, IndexError, orjson.JSONDecodeError):
            price = DEFAULT_PRICE

    return {
        "category": raw.get("category") or "",
        "question": question,
        "volume_24h": volume_24h,
        "days_remaining": days_remaining,
        "spread": spread,
        "price": price,
        "closed": bool(raw.get("closed", False)),
    }


def _bracket_score(value: float, brackets: List[Tuple[float, float]], ascending: bool = False) -> float:
    """Returns the score for the first bracket `value` qualifies for.

    Args:
        value: The metric being scored (volume, days remaining, or spread).
        brackets: (threshold, points) pairs.
        ascending: If False (default), the highest threshold that `value`
            meets or exceeds wins (volume/time-remaining style, bigger is
            better). If True, the lowest threshold `value` is at or below
            wins (spread style, smaller is better).
    """
    if ascending:
        for threshold, points in brackets:
            if value <= threshold:
                return points
        return 0.0
    for threshold, points in brackets:
        if value >= threshold:
            return points
    return 0.0


def _longshot_bonus(price: float) -> float:
    low_min, low_max = LONGSHOT_LOW_RANGE
    high_min, high_max = LONGSHOT_HIGH_RANGE
    if low_min <= price <= low_max or high_min <= price <= high_max:
        return LONGSHOT_BONUS
    return 0.0


def score_market(market: Dict[str, Any]) -> float:
    """Scores a normalized market 0.0-1.0 for insider-trading susceptibility.

    Args:
        market: A dict from normalize_market() with keys category,
            question, volume_24h, days_remaining, spread, price, closed.

    Returns:
        A float in [0.0, 1.0]. 0.0 means "do not track."
    """
    if market.get("closed"):
        return 0.0
    if market["days_remaining"] < MIN_DAYS_REMAINING:
        return 0.0

    text = f"{market.get('category', '')} {market.get('question', '')}".lower()

    if any(keyword in text for keyword in SKIP_CATEGORY_KEYWORDS):
        return 0.0
    if not any(keyword in text for keyword in HIGH_VALUE_CATEGORY_KEYWORDS):
        return 0.0

    score = HIGH_VALUE_CATEGORY_BASE_SCORE
    score += _bracket_score(market["volume_24h"], VOLUME_BRACKETS)
    score += _bracket_score(market["days_remaining"], TIME_BRACKETS)
    score += _bracket_score(market["spread"], SPREAD_BRACKETS, ascending=True)
    score += _longshot_bonus(market["price"])

    return min(score, MAX_SCORE)


TIER1_THRESHOLD = 0.8


def select_tiered_markets(
    scored_markets: List[Dict[str, Any]],
    threshold: float,
    max_total: int,
) -> List[Dict[str, Any]]:
    """Filters and tiers scored markets, enforcing MAX_AUTO_MARKETS.

    Tier 1 (score > TIER1_THRESHOLD) is always included, uncapped -- these
    are never dropped for capacity. Tier 2 (threshold <= score <=
    TIER1_THRESHOLD) fills whatever slots remain up to max_total, highest
    score first.

    Args:
        scored_markets: dicts each containing at least a "score" float key.
        threshold: minimum score for Tier 2 eligibility.
        max_total: hard cap on Tier 1 + Tier 2 combined.

    Returns:
        The qualifying subset of scored_markets, each with a "tier" key
        added (1 or 2). Markets scoring below `threshold` are excluded.
    """
    tier1 = sorted(
        (m for m in scored_markets if m["score"] > TIER1_THRESHOLD),
        key=lambda m: m["score"], reverse=True,
    )
    tier2 = sorted(
        (m for m in scored_markets if threshold <= m["score"] <= TIER1_THRESHOLD),
        key=lambda m: m["score"], reverse=True,
    )

    for m in tier1:
        m["tier"] = 1

    remaining_slots = max(max_total - len(tier1), 0)
    selected_tier2 = tier2[:remaining_slots]
    for m in selected_tier2:
        m["tier"] = 2

    return tier1 + selected_tier2
