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


def _extract_token_ids(market_obj: Dict[str, Any]) -> List[str]:
    """Parses clobTokenIds off a single Gamma market object into a flat list."""
    token_ids = market_obj.get("clobTokenIds")
    if token_ids is None:
        return []
    if isinstance(token_ids, (list, tuple)):
        return [str(t) for t in token_ids]
    if isinstance(token_ids, str):
        try:
            decoded = orjson.loads(token_ids)
            return [str(t) for t in decoded] if isinstance(decoded, (list, tuple)) else [str(decoded)]
        except orjson.JSONDecodeError:
            stripped = token_ids.strip("[]")
            return [p.strip().strip('"').strip("'") for p in stripped.split(",") if p.strip()]
    return [str(token_ids)]


def fetch_candidate_markets() -> List[Dict[str, Any]]:
    """Fetches and merges top-by-volume and recently-opened markets from Gamma.

    Returns a deduplicated (by slug) list of raw market dicts, each carrying
    its raw fields plus flattened "slug" and "token_ids" keys. A failed
    request to one endpoint is logged and skipped -- the cycle still
    proceeds with whatever the other endpoint returned.
    """
    events: Dict[str, Dict[str, Any]] = {}
    for params in (TOP_VOLUME_PARAMS, RECENT_PARAMS):
        try:
            response = requests.get(GAMMA_EVENTS_URL, params=params, timeout=10)
            response.raise_for_status()
            for event in response.json():
                slug = event.get("slug")
                markets = event.get("markets") or []
                if not slug or not markets:
                    continue
                market = markets[0]
                merged = dict(market)
                merged["slug"] = slug
                merged["token_ids"] = _extract_token_ids(market)
                events[slug] = merged
        except Exception as e:
            logger.error(f"Auto-discovery: Gamma API fetch failed ({params.get('order')}): {e}")
    return list(events.values())


def run_discovery_cycle(
    subscribe_callback: Callable[[str, List[str]], None],
    unsubscribe_callback: Callable[[str], None],
    alert_callback: Callable[[str], None],
    redis_client=None,
    session_factory=SessionLocal,
) -> Dict[str, Any]:
    """Runs one full discovery cycle: fetch, score, tier, diff, and apply.

    Args:
        subscribe_callback: called as subscribe_callback(slug, token_ids)
            for each newly qualifying market -- wires into
            GlobalWebSocketManager.add_market.
        unsubscribe_callback: called as unsubscribe_callback(slug) for each
            market that should stop being tracked (resolved after 2
            consecutive misses).
        alert_callback: called with a message string for the Telegram admin
            alert cases (new-market burst, disk thresholds).
        redis_client: optional; used to publish live counters and pre-warm
            market metadata. Failures here are logged and swallowed.
        session_factory: DB session factory; defaults to the app's real
            SessionLocal, overridable in tests.

    Returns:
        {"new": int, "resolved": int, "active": int, "tier1": int,
         "tier2": int, "disk_used_pct": float}
    """
    disk = check_disk_usage()
    gate = disk_gate_level(disk["used_pct"])
    if gate in ("alert", "critical"):
        alert_callback(
            f"{'🚨 DISK ALERT' if gate == 'alert' else '🔴 DISK CRITICAL'}: "
            f"{disk['used_pct']:.1f}% used ({disk['used_gb']:.1f}GB / {disk['total_gb']:.1f}GB)"
        )
    elif gate == "warning":
        alert_callback(
            f"⚠️ DISK WARNING: {disk['used_pct']:.1f}% used "
            f"({disk['used_gb']:.1f}GB / {disk['total_gb']:.1f}GB)\n"
            f"Auto-discovery paused for new Tier 2 subscriptions."
        )

    raw_candidates = fetch_candidate_markets()
    scored: List[Dict[str, Any]] = []
    for raw in raw_candidates:
        normalized = normalize_market(raw)
        if normalized is None:
            continue
        merged = {**raw, **normalized, "score": score_market(normalized)}
        scored.append(merged)

    selected = select_tiered_markets(
        [m for m in scored if m["score"] >= AUTO_DISCOVERY_THRESHOLD],
        threshold=AUTO_DISCOVERY_THRESHOLD,
        max_total=MAX_AUTO_MARKETS,
    )
    if gate != "normal":
        selected = [m for m in selected if m["tier"] == 1]

    selected_by_slug = {m["slug"]: m for m in selected}

    db = session_factory()
    try:
        active_rows = db.query(AutoSubscription).filter_by(status="active").all()
        active_misses = {row.slug: row.consecutive_misses for row in active_rows}
        rows_by_slug = {row.slug: row for row in active_rows}

        diff = diff_discovery_cycle(set(selected_by_slug.keys()), active_misses)
        now = time.time()

        for slug in diff["new_slugs"]:
            market = selected_by_slug[slug]
            token_ids = market.get("token_ids") or []
            if not token_ids:
                continue
            db.add(AutoSubscription(
                slug=slug,
                question=market.get("question"),
                category=market.get("category"),
                market_score=market["score"],
                tier=market["tier"],
                volume_24h=market.get("volume_24h"),
                days_remaining=market.get("days_remaining"),
                token_ids=token_ids,
                subscribed_at=now,
                last_seen_active=now,
                last_cycle_at=now,
                status="active",
                consecutive_misses=0,
            ))
            if redis_client is not None:
                try:
                    redis_client.setex(f"meta:question:{slug}", 86400, market.get("question", "N/A"))
                except Exception as e:
                    logger.error(f"Auto-discovery: failed to pre-warm metadata for {slug}: {e}")
            subscribe_callback(slug, token_ids)
            time.sleep(API_DELAY_SECONDS)

        for slug in diff["kept_slugs"]:
            row = rows_by_slug[slug]
            row.consecutive_misses = 0
            row.last_seen_active = now
            row.last_cycle_at = now

        for slug, misses in diff["missed_slugs"].items():
            row = rows_by_slug[slug]
            row.consecutive_misses = misses
            row.last_cycle_at = now

        for slug in diff["resolved_slugs"]:
            row = rows_by_slug[slug]
            row.status = "resolved"
            row.last_cycle_at = now
            unsubscribe_callback(slug)

        db.commit()

        tier1_count = db.query(AutoSubscription).filter_by(status="active", tier=1).count()
        tier2_count = db.query(AutoSubscription).filter_by(status="active", tier=2).count()
    finally:
        db.close()

    active_count = tier1_count + tier2_count
    summary = {
        "new": len(diff["new_slugs"]),
        "resolved": len(diff["resolved_slugs"]),
        "active": active_count,
        "tier1": tier1_count,
        "tier2": tier2_count,
        "disk_used_pct": disk["used_pct"],
    }

    logger.info(
        f"Auto-discovery cycle complete: +{summary['new']} new, "
        f"-{summary['resolved']} resolved, {summary['active']} active "
        f"(Tier 1: {summary['tier1']}, Tier 2: {summary['tier2']})"
    )

    if redis_client is not None:
        try:
            redis_client.set("auto:markets_total", active_count)
            redis_client.set("auto:markets_tier1", tier1_count)
            redis_client.set("auto:markets_tier2", tier2_count)
            redis_client.set("disk:usage_pct", disk["used_pct"])
            redis_client.incr("auto:cycles_total")
        except Exception as e:
            logger.error(f"Auto-discovery: failed to publish Redis counters: {e}")

    if summary["new"] > 10:
        alert_callback(
            f"🔍 Auto-discovery: +{summary['new']} new markets "
            f"(Tier 1: {tier1_count}, Tier 2: {tier2_count}, Total: {active_count})"
        )

    return summary
