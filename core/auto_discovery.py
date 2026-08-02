"""Background scheduler that discovers, scores, and tracks high-value
Polymarket markets automatically. See reference/auto_discovery.md (Step 8.5c).

Deliberately does not import app.py (would create a circular import) --
subscribing, unsubscribing, and alerting are all injected as callables by
whoever starts run_scheduler_loop (app.py's lifespan).
"""

import logging
import os
import shutil
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import orjson
import requests

from analytics.market_scorer import normalize_market, score_market, select_tiered_markets
from db import SessionLocal
from models.orm import AutoSubscription

logger = logging.getLogger("polymarket.core.auto_discovery")

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
TOP_VOLUME_PARAMS = {"active": "true", "order": "volume24Hr", "ascending": "false", "limit": 100}
RECENT_PARAMS = {"active": "true", "order": "startDate", "ascending": "false", "limit": 50}
RESOLVED_MISS_THRESHOLD = 2

AUTO_DISCOVERY_ENABLED = os.getenv("AUTO_DISCOVERY_ENABLED", "true").lower() == "true"
AUTO_DISCOVERY_INTERVAL_HOURS = float(os.getenv("AUTO_DISCOVERY_INTERVAL_HOURS", "6"))
AUTO_DISCOVERY_THRESHOLD = float(os.getenv("AUTO_DISCOVERY_THRESHOLD", "0.5"))
MAX_AUTO_MARKETS = int(os.getenv("MAX_AUTO_MARKETS", "500"))
API_DELAY_SECONDS = float(os.getenv("AUTO_DISCOVERY_API_DELAY_SECONDS", "0.1"))

DISK_WARNING_PCT = float(os.getenv("DISK_WARNING_PCT", "70"))
DISK_ALERT_PCT = float(os.getenv("DISK_ALERT_PCT", "80"))
DISK_CRITICAL_PCT = float(os.getenv("DISK_CRITICAL_PCT", "90"))


def check_disk_usage(path: str = "/") -> Dict[str, float]:
    """Returns current disk usage stats for the given path.

    Returns:
        {"used_pct": float, "used_gb": float, "total_gb": float}
    """
    total, used, _free = shutil.disk_usage(path)
    return {
        "used_pct": (used / total) * 100.0 if total else 0.0,
        "used_gb": used / (1024 ** 3),
        "total_gb": total / (1024 ** 3),
    }


def disk_gate_level(used_pct: float) -> str:
    """Classifies disk usage into normal/warning/alert/critical per env thresholds.

    Only "warning" and above pause new Tier 2 subscriptions -- Tier 1
    markets are never gated by disk pressure at any level (per
    reference/auto_discovery.md's testing checklist).
    """
    if used_pct >= DISK_CRITICAL_PCT:
        return "critical"
    if used_pct >= DISK_ALERT_PCT:
        return "alert"
    if used_pct >= DISK_WARNING_PCT:
        return "warning"
    return "normal"


def diff_discovery_cycle(selected_slugs: set, active_misses: Dict[str, int]) -> Dict[str, Any]:
    """Computes which markets are new, kept, missed, or resolved this cycle.

    A market absent from `selected_slugs` is not dropped immediately -- its
    consecutive_misses counter increments, and it's only reported as
    resolved once that counter reaches RESOLVED_MISS_THRESHOLD.

    Args:
        selected_slugs: Slugs that qualified (scored + tiered) this cycle.
        active_misses: Mapping of slug -> current consecutive_misses, for
            every row currently `status='active'` in the DB.

    Returns:
        {"new_slugs": set, "kept_slugs": set,
         "missed_slugs": Dict[str, int] (new miss count, still < threshold),
         "resolved_slugs": set (miss count reached threshold)}
    """
    active_slugs = set(active_misses.keys())
    new_slugs = selected_slugs - active_slugs
    kept_slugs = selected_slugs & active_slugs
    missing_slugs = active_slugs - selected_slugs

    missed_slugs: Dict[str, int] = {}
    resolved_slugs = set()
    for slug in missing_slugs:
        misses = active_misses[slug] + 1
        if misses >= RESOLVED_MISS_THRESHOLD:
            resolved_slugs.add(slug)
        else:
            missed_slugs[slug] = misses

    return {
        "new_slugs": new_slugs,
        "kept_slugs": kept_slugs,
        "missed_slugs": missed_slugs,
        "resolved_slugs": resolved_slugs,
    }
