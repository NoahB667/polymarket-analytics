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

from analytics.market_scorer import (
    backfill_to_minimum,
    normalize_market,
    score_market,
    select_tiered_markets,
)
from db import SessionLocal
from models.orm import AutoSubscription

logger = logging.getLogger("polymarket.core.auto_discovery")

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
# Gamma silently caps a single /events response at 100 rows regardless of the
# requested `limit`, so reaching a broader candidate pool requires paginating
# via `offset` rather than asking for a bigger limit.
TOP_VOLUME_PAGE_SIZE = 100
# Measured live against the real Gamma feed: ~7-8% of raw candidates pass the
# category hard filter (most of Polymarket's volume is sports/esports). 10
# pages (~1000 raw) reliably yields ~70-80 category-relevant candidates,
# comfortably clearing a MIN_ACTIVE_MARKETS=50 floor with margin.
TOP_VOLUME_PAGES = 10
TOP_VOLUME_PARAMS = {"active": "true", "order": "volume24hr", "ascending": "false", "limit": TOP_VOLUME_PAGE_SIZE}
RECENT_PARAMS = {"active": "true", "order": "startDate", "ascending": "false", "limit": 100}
RESOLVED_MISS_THRESHOLD = 2
CATEGORY_MAX_LENGTH = 500  # must stay <= AutoSubscription.category's VARCHAR width

AUTO_DISCOVERY_ENABLED = os.getenv("AUTO_DISCOVERY_ENABLED", "true").lower() == "true"
AUTO_DISCOVERY_INTERVAL_HOURS = float(os.getenv("AUTO_DISCOVERY_INTERVAL_HOURS", "2"))
AUTO_DISCOVERY_THRESHOLD = float(os.getenv("AUTO_DISCOVERY_THRESHOLD", "0.5"))
MAX_AUTO_MARKETS = int(os.getenv("MAX_AUTO_MARKETS", "500"))
MIN_ACTIVE_MARKETS = int(os.getenv("MIN_ACTIVE_MARKETS", "50"))
API_DELAY_SECONDS = float(os.getenv("AUTO_DISCOVERY_API_DELAY_SECONDS", "0.1"))
NEW_MARKET_ALERT_THRESHOLD = 10

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


def _extract_outcome_labels(market_obj: Dict[str, Any]) -> List[str]:
    """Parses the "outcomes" field off a single Gamma market object into a flat list.

    Gamma returns this as a JSON-encoded string array (e.g. '["Yes", "No"]')
    positionally parallel to clobTokenIds -- mirrors _extract_token_ids'
    parsing conventions for the same encoding quirks.
    """
    outcomes = market_obj.get("outcomes")
    if outcomes is None:
        return []
    if isinstance(outcomes, (list, tuple)):
        return [str(o) for o in outcomes]
    if isinstance(outcomes, str):
        try:
            decoded = orjson.loads(outcomes)
            return [str(o) for o in decoded] if isinstance(decoded, (list, tuple)) else [str(decoded)]
        except orjson.JSONDecodeError:
            stripped = outcomes.strip("[]")
            return [p.strip().strip('"').strip("'") for p in stripped.split(",") if p.strip()]
    return [str(outcomes)]


def _extract_category(event: Dict[str, Any]) -> str:
    """Derives a category string from a Gamma event's tags.

    Neither the event nor its nested market objects expose a flat
    "category" field on the live API (verified directly -- both are always
    None) despite reference/auto_discovery.md assuming one exists. The real
    topic signal lives in event["tags"], a list of {"label": ..., "slug":
    ...} objects (e.g. a market tagged "Sports"/"MLB" carries a tag with
    label "Sports" even when the question text itself never says "MLB").
    Joining the labels reconstructs an equivalent to what score_market's
    keyword matching was designed to scan.
    """
    tags = event.get("tags") or []
    labels = [tag.get("label") for tag in tags if isinstance(tag, dict) and tag.get("label")]
    joined = ", ".join(labels)
    # Defensive cap independent of the DB column width: a market with many
    # tags produced a string long enough to fail the AutoSubscription.category
    # column's VARCHAR limit and roll back an entire cycle's bulk insert --
    # truncate here so scoring (keyword substring matching) still works fine
    # on a prefix, regardless of how the column is sized.
    return joined[:CATEGORY_MAX_LENGTH]


def _merge_events_page(events: Dict[str, Dict[str, Any]], raw_events: List[Dict[str, Any]]) -> None:
    """Flattens a page of Gamma /events results into `events`, keyed by slug."""
    for event in raw_events:
        slug = event.get("slug")
        markets = event.get("markets") or []
        if not slug or not markets:
            continue
        market = markets[0]
        merged = dict(market)
        merged["slug"] = slug
        merged["token_ids"] = _extract_token_ids(market)
        merged["category"] = _extract_category(event)
        events[slug] = merged


def fetch_candidate_markets() -> List[Dict[str, Any]]:
    """Fetches and merges top-by-volume and recently-opened markets from Gamma.

    Gamma caps a single /events response at 100 rows regardless of the
    requested `limit`, so the top-by-volume source is paginated via `offset`
    across TOP_VOLUME_PAGES pages to reach a broader candidate pool -- this
    matters for MIN_ACTIVE_MARKETS backfill, which needs enough raw
    candidates to find category-relevant markets even below the normal
    quality threshold. A failed page is logged and the loop stops early
    (page requests are sequential/ordered by volume, so a failure partway
    through still leaves the highest-volume pages already collected); the
    cycle still proceeds with whatever was gathered.

    Returns a deduplicated (by slug) list of raw market dicts, each carrying
    its raw fields plus flattened "slug" and "token_ids" keys.
    """
    events: Dict[str, Dict[str, Any]] = {}

    for page in range(TOP_VOLUME_PAGES):
        params = {**TOP_VOLUME_PARAMS, "offset": page * TOP_VOLUME_PAGE_SIZE}
        try:
            response = requests.get(GAMMA_EVENTS_URL, params=params, timeout=10)
            response.raise_for_status()
            page_events = response.json()
            if not page_events:
                break
            _merge_events_page(events, page_events)
            if len(page_events) < TOP_VOLUME_PAGE_SIZE:
                break
        except Exception as e:
            logger.error(f"Auto-discovery: Gamma API fetch failed (volume24hr page {page}): {e}")
            break

    try:
        response = requests.get(GAMMA_EVENTS_URL, params=RECENT_PARAMS, timeout=10)
        response.raise_for_status()
        _merge_events_page(events, response.json())
    except Exception as e:
        logger.error(f"Auto-discovery: Gamma API fetch failed (startDate): {e}")

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
    else:
        selected = backfill_to_minimum(selected, scored, MIN_ACTIVE_MARKETS, MAX_AUTO_MARKETS)

    selected_by_slug = {m["slug"]: m for m in selected}

    db = session_factory()
    try:
        active_rows = db.query(AutoSubscription).filter_by(status="active").all()
        active_misses = {row.slug: row.consecutive_misses for row in active_rows}
        rows_by_slug = {row.slug: row for row in active_rows}

        diff = diff_discovery_cycle(set(selected_by_slug.keys()), active_misses)
        now = time.time()

        applied_new = set()
        for slug in diff["new_slugs"]:
            try:
                market = selected_by_slug[slug]
                token_ids = market.get("token_ids") or []
                if not token_ids:
                    continue
                # Call the real-world side effect FIRST -- only persist the
                # AutoSubscription row if subscribe_callback actually
                # succeeded, so DB state never claims a market is "active"
                # when GlobalWebSocketManager never picked it up.
                subscribe_callback(slug, token_ids)
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
                        market_id = market.get("conditionId")
                        if not market_id:
                            logger.error(
                                f"Auto-discovery: missing conditionId for {slug}, "
                                f"cannot pre-warm metadata (reads are keyed on market id, not slug)"
                            )
                        else:
                            redis_client.setex(
                                f"meta:question:{market_id}", 86400, market.get("question", "N/A")
                            )
                            outcome_labels = _extract_outcome_labels(market)
                            if outcome_labels and len(outcome_labels) == len(token_ids):
                                outcomes_map = dict(zip(token_ids, outcome_labels))
                                outcomes_hash_key = f"meta:outcomes:{market_id}"
                                pipe = redis_client.pipeline()
                                pipe.delete(outcomes_hash_key)
                                pipe.hset(outcomes_hash_key, mapping=outcomes_map)
                                pipe.expire(outcomes_hash_key, 86400)
                                pipe.execute()
                            else:
                                logger.error(
                                    f"Auto-discovery: outcome/token_id count mismatch for {slug} "
                                    f"({len(outcome_labels)} outcomes vs {len(token_ids)} token_ids), "
                                    f"skipping outcomes pre-warm"
                                )
                    except Exception as e:
                        logger.error(f"Auto-discovery: failed to pre-warm metadata for {slug}: {e}")
                time.sleep(API_DELAY_SECONDS)
                applied_new.add(slug)
            except Exception as e:
                logger.error(f"Auto-discovery: failed to process new market {slug}: {e}")
                continue

        for slug in diff["kept_slugs"]:
            try:
                row = rows_by_slug[slug]
                row.consecutive_misses = 0
                row.last_seen_active = now
                row.last_cycle_at = now
            except Exception as e:
                logger.error(f"Auto-discovery: failed to update kept market {slug}: {e}")
                continue

        for slug, misses in diff["missed_slugs"].items():
            try:
                row = rows_by_slug[slug]
                row.consecutive_misses = misses
                row.last_cycle_at = now
            except Exception as e:
                logger.error(f"Auto-discovery: failed to update missed market {slug}: {e}")
                continue

        for slug in diff["resolved_slugs"]:
            try:
                row = rows_by_slug[slug]
                # Call the real-world side effect FIRST -- only mark the row
                # resolved if unsubscribe_callback actually succeeded, so a
                # still-live market never gets left permanently mislabeled
                # "resolved" in the DB while GlobalWebSocketManager still
                # has it subscribed.
                unsubscribe_callback(slug)
                row.status = "resolved"
                row.last_cycle_at = now
            except Exception as e:
                logger.error(f"Auto-discovery: failed to resolve market {slug}: {e}")
                continue

        try:
            db.commit()
        except Exception as e:
            logger.error(f"Auto-discovery: failed to commit discovery cycle: {e}")
            db.rollback()
            applied_new = set()

        tier1_count = db.query(AutoSubscription).filter_by(status="active", tier=1).count()
        tier2_count = db.query(AutoSubscription).filter_by(status="active", tier=2).count()
        tier3_count = db.query(AutoSubscription).filter_by(status="active", tier=3).count()
    finally:
        db.close()

    active_count = tier1_count + tier2_count + tier3_count
    summary = {
        "new": len(applied_new),
        "resolved": len(diff["resolved_slugs"]),
        "active": active_count,
        "tier1": tier1_count,
        "tier2": tier2_count,
        "tier3": tier3_count,
        "disk_used_pct": disk["used_pct"],
    }

    logger.info(
        f"Auto-discovery cycle complete: +{summary['new']} new, "
        f"-{summary['resolved']} resolved, {summary['active']} active "
        f"(Tier 1: {summary['tier1']}, Tier 2: {summary['tier2']}, Floor: {summary['tier3']})"
    )

    if redis_client is not None:
        try:
            redis_client.set("auto:markets_total", active_count)
            redis_client.set("auto:markets_tier1", tier1_count)
            redis_client.set("auto:markets_tier2", tier2_count)
            redis_client.set("auto:markets_tier3", tier3_count)
            redis_client.set("disk:usage_pct", disk["used_pct"])
            redis_client.incr("auto:cycles_total")
        except Exception as e:
            logger.error(f"Auto-discovery: failed to publish Redis counters: {e}")

    if summary["new"] > NEW_MARKET_ALERT_THRESHOLD:
        alert_callback(
            f"🔍 Auto-discovery: +{summary['new']} new markets "
            f"(Tier 1: {tier1_count}, Tier 2: {tier2_count}, Floor: {tier3_count}, Total: {active_count})"
        )

    return summary


def restore_active_subscriptions(
    subscribe_callback: Callable[[str, List[str]], None],
    session_factory=SessionLocal,
) -> int:
    """Restores all active auto-tracked markets from the DB on startup.

    Uses stored token_ids -- zero API calls in the common case. Falls back
    to a fresh Gamma lookup (with a defensive sleep between calls) only for
    rows missing token_ids, which should be rare.

    Returns:
        The number of markets restored (subscribe_callback invoked).
    """
    db = session_factory()
    try:
        rows = db.query(AutoSubscription).filter_by(status="active").all()
        restored = 0
        for row in rows:
            try:
                token_ids = row.token_ids
                if not token_ids:
                    try:
                        response = requests.get(f"{GAMMA_EVENTS_URL}?slug={row.slug}", timeout=10)
                        response.raise_for_status()
                        data = response.json()
                        if data and data[0].get("markets"):
                            token_ids = _extract_token_ids(data[0]["markets"][0])
                            row.token_ids = token_ids
                    except Exception as e:
                        logger.error(f"Auto-discovery: failed to refresh token_ids for {row.slug}: {e}")
                        continue
                    time.sleep(API_DELAY_SECONDS)
                if token_ids:
                    subscribe_callback(row.slug, token_ids)
                    restored += 1
            except Exception as e:
                logger.error(f"Auto-discovery: failed to restore {row.slug}, skipping: {e}")
                continue
        db.commit()
        return restored
    finally:
        db.close()


def run_scheduler_loop(
    subscribe_callback: Callable[[str, List[str]], None],
    unsubscribe_callback: Callable[[str], None],
    alert_callback: Callable[[str], None],
    redis_client=None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Restores prior state, runs an immediate cycle, then loops on schedule.

    Intended as the target of a single daemon thread started from app.py's
    lifespan. Never raises -- any cycle failure is logged and the loop
    continues, so a transient Gamma API outage never permanently kills
    discovery.
    """
    if not AUTO_DISCOVERY_ENABLED:
        logger.info("Auto-discovery disabled via AUTO_DISCOVERY_ENABLED=false")
        return

    stop_event = stop_event or threading.Event()

    try:
        restored = restore_active_subscriptions(subscribe_callback)
        logger.info(f"Auto-discovery: restored {restored} active markets from DB on startup")
    except Exception as e:
        logger.error(f"Auto-discovery: startup restore failed: {e}")

    while not stop_event.is_set():
        try:
            run_discovery_cycle(subscribe_callback, unsubscribe_callback, alert_callback, redis_client)
        except Exception as e:
            logger.error(f"Auto-discovery: cycle failed, will retry next interval: {e}")
        stop_event.wait(AUTO_DISCOVERY_INTERVAL_HOURS * 3600)
