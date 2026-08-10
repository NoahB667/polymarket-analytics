"""Alert message templates (reference/signal_design.md "Channel Broadcast
Logic"). Enforces the "surveillance, not signals" language rules: never
say buy/sell/trade/recommend/predict/etc, always end with the disclaimer.
"""

import re
import time
from typing import List

from models.orm import AnomalyEvent

FORBIDDEN_WORDS = [
    "buy", "sell", "trade", "invest", "position", "recommend", "suggest",
    "should", "predict", "profit", "gain", "opportunity", "edge", "signal", "will",
]

DISCLAIMER = "Market surveillance only. Not financial advice."


def contains_forbidden_language(text: str) -> List[str]:
    """Returns every forbidden word found in text (case-insensitive, word-boundary)."""
    lowered = text.lower()
    return [word for word in FORBIDDEN_WORDS if re.search(rf"\b{re.escape(word)}\b", lowered)]


def _hashtag(category: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (category or "market").lower())
    return f"#{slug or 'market'}"


def _time_ago(timestamp: float) -> str:
    minutes = max(0, int((time.time() - timestamp) / 60))
    if minutes < 1:
        return "moments ago"
    if minutes == 1:
        return "1 minute ago"
    return f"{minutes} minutes ago"


def format_premium_alert(event: AnomalyEvent, daily_volume: float = 0.0) -> str:
    """Full-context alert for the premium channel."""
    lines = [
        f"UNUSUAL ACTIVITY -- {event.question}",
        "",
        f"Price: ${event.current_price:.2f} ({event.current_price * 100:.0f}% implied)",
        f"Movement: {event.price_change_pct:+.1f}% in last 20 minutes",
        "",
        "What we're observing:",
        f"- {event.buy_pressure_pct:.0f}% of volume one-directional (last 15min)",
        f"- Volume {event.volume_spike_ratio:.1f}x above this market's 24h average",
    ]
    if event.is_long_shot:
        lines.append(f"- Long-shot territory -- {event.current_price * 100:.0f}% implied probability")
    if event.wallet_context_available and event.anomalous_wallet_count > 0:
        lines.append(
            f"- {event.anomalous_wallet_count} wallets with historically anomalous "
            "patterns newly active on this market"
        )
    lines += [
        "",
        f"Activity began {_time_ago(event.timestamp)}",
        f"Total market volume today: ${daily_volume:,.0f}",
        "",
        DISCLAIMER,
        f"{_hashtag(event.category)}",
    ]
    return "\n".join(lines)


def format_free_alert(event: AnomalyEvent) -> str:
    """Basic-context alert for the free channel -- no OFI numbers, no wallet context."""
    return "\n".join([
        "Unusual activity detected",
        "",
        f"Market: {event.question}",
        f"Price: ${event.current_price:.2f} ({event.current_price * 100:.0f}% implied)",
        "",
        "Unusual order flow patterns observed.",
        "Full analysis in premium channel.",
        "",
        DISCLAIMER,
    ])
