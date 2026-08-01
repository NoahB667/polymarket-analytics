"""Pure statistical layer for wallet behavioral profiling and insider-score calculation.

Isolated from I/O (no DB or Redis calls) so it can be reused by bulk backfill
processing and, later, live streaming hooks alike. All persistence lives in
blockchain/wallet_profiler.py.
"""

import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models.dataclasses import WalletProfile

# -- Category classification -------------------------------------------------
CATEGORY_KEYWORDS = ["fed", "rate", "election", "crypto", "sec", "peace", "israel", "iran", "taiwan"]
DEFAULT_CATEGORY = "other"
TOP_CATEGORIES_LIMIT = 3

# -- Profile thresholds --------------------------------------------------------
LONG_SHOT_PRICE_THRESHOLD = 0.20
NEW_ACCOUNT_MAX_AGE_DAYS = 30.0
UNKNOWN_RESOLUTION_PROXIMITY_DAYS = 999.0  # sentinel: no market_end_time data available

# -- Insider score formula (reference/signal_design.md) -----------------------
MIN_LONGSHOT_SAMPLE_SIZE = 5
LONGSHOT_EXCESS_MULTIPLIER = 2.0
LONGSHOT_EXCESS_WEIGHT = 0.40
CATEGORY_CONCENTRATION_WEIGHT = 0.20
NEW_ACCOUNT_MIN_BET_USD = 1000.0
NEW_ACCOUNT_LARGE_BET_WEIGHT = 0.20
RESOLUTION_PROXIMITY_MAX_DAYS = 1.0
RESOLUTION_PROXIMITY_WEIGHT = 0.10
FEW_TRADES_MAX_COUNT = 10
FEW_TRADES_MIN_WIN_RATE = 0.70
FEW_TRADES_HIGH_WINRATE_WEIGHT = 0.10


def classify_category(event_market_name: Optional[str]) -> str:
    """Buckets a market's event name into a coarse topic sector via keyword match.

    Args:
        event_market_name: The Dune `event_market_name` field, e.g.
            "Presidential election 2024". May be None.

    Returns:
        The first matching keyword sector, or DEFAULT_CATEGORY if none match.
    """
    name = (event_market_name or "").lower()
    for keyword in CATEGORY_KEYWORDS:
        if keyword in name:
            return keyword
    return DEFAULT_CATEGORY


def compile_profile(wallet_address: str, trades: List[Dict[str, Any]]) -> WalletProfile:
    """Aggregates raw on-chain trade rows into a WalletProfile.

    Args:
        wallet_address: The wallet's on-chain address.
        trades: Trade dicts with keys market_id, category, entry_price, outcome,
            resolved_outcome (Optional[str]), usd_volume, block_timestamp,
            market_end_time (Optional[float]).

    Returns:
        A WalletProfile with insider_score left at 0.0 — call
        calculate_insider_score() separately to score it.
    """
    if not trades:
        return WalletProfile(
            wallet_address=wallet_address,
            first_trade_date=datetime.now(timezone.utc),
            total_trades=0,
            unique_markets=0,
            longshot_attempts=0,
            longshot_wins=0,
            longshot_win_rate=0.0,
            avg_implied_prob_at_entry=0.0,
            top_categories=[],
            category_concentration=0.0,
            avg_days_before_resolution=UNKNOWN_RESOLUTION_PROXIMITY_DAYS,
            new_account_flag=False,
            avg_bet_size=0.0,
        )

    total_trades = len(trades)
    unique_markets = len(set(t["market_id"] for t in trades))

    total_usd_volume = sum(float(t.get("usd_volume", 0.0)) for t in trades)
    avg_bet_size = total_usd_volume / total_trades

    timestamps = [float(t["block_timestamp"]) for t in trades]
    first_trade_timestamp = min(timestamps)
    first_trade_date = datetime.fromtimestamp(first_trade_timestamp, tz=timezone.utc)
    account_age_days = max(0.0, (time.time() - first_trade_timestamp) / 86400.0)
    new_account_flag = account_age_days < NEW_ACCOUNT_MAX_AGE_DAYS

    category_counts: Counter = Counter()
    longshot_attempts = 0
    longshot_wins = 0
    longshot_entry_prices: List[float] = []
    resolution_gaps_days: List[float] = []

    for trade in trades:
        category_counts[trade.get("category") or DEFAULT_CATEGORY] += 1

        entry_price = float(trade.get("entry_price", 1.0))
        if entry_price < LONG_SHOT_PRICE_THRESHOLD:
            longshot_attempts += 1
            longshot_entry_prices.append(entry_price)
            resolved_outcome = trade.get("resolved_outcome")
            if resolved_outcome is not None:
                outcome = str(trade.get("outcome", "")).strip().lower()
                if outcome == str(resolved_outcome).strip().lower():
                    longshot_wins += 1

        market_end_time = trade.get("market_end_time")
        if market_end_time is not None:
            gap_days = (float(market_end_time) - float(trade["block_timestamp"])) / 86400.0
            resolution_gaps_days.append(gap_days)

    longshot_win_rate = longshot_wins / longshot_attempts if longshot_attempts > 0 else 0.0
    avg_implied_prob_at_entry = (
        sum(longshot_entry_prices) / len(longshot_entry_prices) if longshot_entry_prices else 0.0
    )

    top_categories = [c for c, _ in category_counts.most_common(TOP_CATEGORIES_LIMIT)]
    max_category_count = max(category_counts.values()) if category_counts else 0
    category_concentration = max_category_count / total_trades if total_trades > 0 else 0.0

    avg_days_before_resolution = (
        sum(resolution_gaps_days) / len(resolution_gaps_days)
        if resolution_gaps_days
        else UNKNOWN_RESOLUTION_PROXIMITY_DAYS
    )

    return WalletProfile(
        wallet_address=wallet_address,
        first_trade_date=first_trade_date,
        total_trades=total_trades,
        unique_markets=unique_markets,
        longshot_attempts=longshot_attempts,
        longshot_wins=longshot_wins,
        longshot_win_rate=round(longshot_win_rate, 4),
        avg_implied_prob_at_entry=round(avg_implied_prob_at_entry, 4),
        top_categories=top_categories,
        category_concentration=round(category_concentration, 4),
        avg_days_before_resolution=round(avg_days_before_resolution, 2),
        new_account_flag=new_account_flag,
        avg_bet_size=round(avg_bet_size, 2),
    )


def calculate_insider_score(profile: WalletProfile) -> float:
    """Scores a WalletProfile's insider risk in [0.0, 1.0] via the 5-component formula.

    Formula per reference/signal_design.md, with the [0.0, 1.0] clamp from
    CLAUDE.md's quick-reference applied to the final total (the spec's own
    pseudocode only caps the upper bound; a negative-scoring long-shot excess
    should not be allowed to make insider risk negative).

    Mutates profile.insider_score and profile.score_components with a
    per-component breakdown for transparency, and returns the final score.
    """
    components: Dict[str, float] = {}
    score = 0.0

    if profile.longshot_attempts >= MIN_LONGSHOT_SAMPLE_SIZE:
        excess = profile.longshot_win_rate - profile.avg_implied_prob_at_entry
        components["longshot_excess"] = min(excess * LONGSHOT_EXCESS_MULTIPLIER, LONGSHOT_EXCESS_WEIGHT)
    else:
        components["longshot_excess"] = 0.0
    score += components["longshot_excess"]

    components["category_concentration"] = profile.category_concentration * CATEGORY_CONCENTRATION_WEIGHT
    score += components["category_concentration"]

    if profile.new_account_flag and profile.avg_bet_size > NEW_ACCOUNT_MIN_BET_USD:
        components["new_account_large_bet"] = NEW_ACCOUNT_LARGE_BET_WEIGHT
    else:
        components["new_account_large_bet"] = 0.0
    score += components["new_account_large_bet"]

    if profile.avg_days_before_resolution < RESOLUTION_PROXIMITY_MAX_DAYS:
        components["resolution_proximity"] = RESOLUTION_PROXIMITY_WEIGHT
    else:
        components["resolution_proximity"] = 0.0
    score += components["resolution_proximity"]

    if profile.total_trades < FEW_TRADES_MAX_COUNT and profile.longshot_win_rate > FEW_TRADES_MIN_WIN_RATE:
        components["few_trades_high_winrate"] = FEW_TRADES_HIGH_WINRATE_WEIGHT
    else:
        components["few_trades_high_winrate"] = 0.0
    score += components["few_trades_high_winrate"]

    final_score = max(0.0, min(round(score, 4), 1.0))
    profile.score_components = components
    profile.insider_score = final_score
    return final_score
