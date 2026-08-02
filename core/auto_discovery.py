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
