"""I/O orchestration for the AnomalyEvent -> broadcaster path
(reference/PROJECT_CONTEXT.md "Core Concept: AnomalyEvent"). Reads cached
Signal 1 (Redis) and Signal 2 (blockchain.wallet_profiler) for every active
auto-tracked market, evaluates signal_core.detection.anomaly_detector,
persists append-only AnomalyEvent rows, applies the Redis-backed cooldown
gate, schedules price-impact checks for HIGH/CRITICAL events, and hands
generated events to a broadcast callback.

Not on the WebSocket hot path -- runs on its own polling thread started
from app.py's lifespan, mirroring analytics/signal_combiner.py exactly.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from analytics.order_flow import calculate_price_change_pct
from analytics.signal_combiner import read_signal1
from blockchain.wallet_profiler import build_signal2_score
from models.orm import AnomalyEvent, AutoSubscription, PriceImpactCheck
from signal_core.config.signal_params import ALERT_COOLDOWN_MINUTES
from signal_core.detection.anomaly_detector import SEVERITY_CRITICAL, evaluate_anomaly

logger = logging.getLogger("polymarket.analytics.anomaly_engine")

ALERT_COOLDOWN_SECONDS = ALERT_COOLDOWN_MINUTES * 60.0

# (checkpoint_interval, offset_seconds) -- reference/signal_design.md
# "Private Analysis: Price Impact Tracking".
PRICE_IMPACT_CHECKPOINTS = [
    ("5m", 5 * 60),
    ("15m", 15 * 60),
    ("1h", 60 * 60),
    ("4h", 4 * 60 * 60),
    ("24h", 24 * 60 * 60),
]

HIGH_SEVERITIES = {"HIGH", SEVERITY_CRITICAL}


def build_anomaly_event(
    db: Any,
    redis_client: Any,
    auto_subscription: AutoSubscription,
) -> Optional[AnomalyEvent]:
    """Evaluates one market and returns an unsaved AnomalyEvent, or None if
    no trigger condition fired (see evaluate_anomaly's contract).
    """
    signal1 = read_signal1(redis_client, auto_subscription.slug)
    if signal1 is None:
        return None

    if auto_subscription.condition_id:
        signal2 = build_signal2_score(db, auto_subscription.condition_id, redis_client)
        wallet_context_available = True
    else:
        signal2 = None
        wallet_context_available = False

    latest_price = signal1.get("latest_price", 0.0)
    result = evaluate_anomaly(
        signal1=signal1,
        signal2_market_insider_risk=signal2.market_insider_risk if signal2 else 0.0,
        signal2_confidence=signal2.confidence if signal2 else 0.0,
        signal2_high_score_wallet_count=signal2.high_score_wallet_count if signal2 else 0,
        latest_price=latest_price,
    )
    if result.trigger is None:
        return None

    return AnomalyEvent(
        market_id=auto_subscription.condition_id or auto_subscription.slug,
        slug=auto_subscription.slug,
        question=auto_subscription.question,
        category=auto_subscription.category,
        timestamp=time.time(),
        trigger=result.trigger,
        severity=result.severity,
        anomaly_score=result.anomaly_score,
        current_price=latest_price,
        price_change_pct=calculate_price_change_pct(auto_subscription.slug),
        ofi_15min=signal1["metrics"]["ofi_15m"],
        volume_spike_ratio=signal1["volume_spike_ratio"],
        is_long_shot=result.is_long_shot,
        buy_pressure_pct=result.buy_pressure_pct,
        anomalous_wallet_count=signal2.high_score_wallet_count if signal2 else 0,
        market_insider_risk=signal2.market_insider_risk if signal2 else 0.0,
        wallet_context_available=wallet_context_available,
        broadcast_free=result.broadcast_free,
        broadcast_premium=result.broadcast_premium,
        broadcast_reason=result.broadcast_reason,
    )


def _cooldown_active(redis_client: Any, market_id: str) -> bool:
    try:
        last = redis_client.get(f"alert:last:{market_id}")
    except Exception:
        return False
    if last is None:
        return False
    last_ts = float(last)
    return (time.time() - last_ts) < ALERT_COOLDOWN_SECONDS


def _schedule_price_impact_checks(db: Any, event: AnomalyEvent, asset_id: str) -> None:
    """HIGH/CRITICAL events get price-impact checks at every checkpoint
    (reference/signal_design.md). Best-effort -- never raises.

    Uses flush(), not commit(): the caller (run_anomaly_engine_cycle) commits
    exactly once after broadcast_fn has also run, so the still-uncommitted
    AnomalyEvent row is never durably inserted before its final state (e.g.
    posted_at_premium/posted_at_free set by broadcast_fn) is known -- that
    would otherwise force a later UPDATE, violating the anomaly_event table's
    append-only invariant (models/orm.py).
    """
    if event.severity not in HIGH_SEVERITIES:
        return
    try:
        now = time.time()
        for interval, offset in PRICE_IMPACT_CHECKPOINTS:
            db.add(PriceImpactCheck(
                slug=event.slug, market_id=event.market_id, asset_id=asset_id,
                anomaly_event_id=event.id,
                direction="BUY", entry_price=event.current_price, entry_time=now,
                checkpoint_interval=interval, target_check_time=now + offset,
            ))
        db.flush()
    except Exception as e:
        db.rollback()
        logger.error(f"Anomaly engine: failed to schedule price impact checks for {event.slug}: {e}")


def run_anomaly_engine_cycle(
    session_factory: Callable[[], Any],
    redis_client: Any,
    broadcast_fn: Callable[[Any, AnomalyEvent], None],
) -> dict:
    """Runs one full cycle over every active auto-tracked market.

    Best-effort per market: a failure evaluating one market is logged and
    skipped, it does not stop the cycle (CLAUDE.md rule 3).
    """
    summary = {"evaluated": 0, "generated": 0}
    db = session_factory()
    try:
        active_rows = db.query(AutoSubscription).filter_by(status="active").all()
        for row in active_rows:
            try:
                event = build_anomaly_event(db, redis_client, row)
                if event is None:
                    continue
                summary["evaluated"] += 1

                if event.severity != SEVERITY_CRITICAL and _cooldown_active(redis_client, event.market_id):
                    continue

                # broadcast_fn mutates event.posted_at_premium/posted_at_free
                # in place (channel/broadcaster.py dispatch()). It MUST run
                # before event is ever added to the session: once a row has
                # been flushed, SQLAlchemy treats it as persistent and any
                # later attribute change forces a real UPDATE statement on
                # the next flush, no matter how commits are arranged --
                # violating anomaly_event's append-only invariant
                # (models/orm.py). A broadcast failure must not lose the
                # event, so it's isolated in its own try/except (matches the
                # best-effort discipline already used for the cooldown key
                # and _schedule_price_impact_checks below).
                try:
                    broadcast_fn(db, event)
                except Exception as be:
                    logger.error(f"Anomaly engine: broadcast failed for {event.slug}: {be}")

                db.add(event)
                db.flush()  # assigns event.id, single INSERT with final state
                summary["generated"] += 1

                try:
                    redis_client.setex(f"alert:last:{event.market_id}", int(ALERT_COOLDOWN_SECONDS), str(time.time()))
                except Exception as re:
                    logger.error(f"Anomaly engine: failed to set cooldown key for {event.slug}: {re}")

                asset_id = (row.token_ids or [""])[0]
                _schedule_price_impact_checks(db, event, asset_id)

                db.commit()  # single commit -- event row is never touched again after its INSERT
            except Exception as e:
                db.rollback()
                logger.error(f"Anomaly engine: failed to evaluate {row.slug}: {e}")
                continue
    finally:
        db.close()

    logger.info(
        f"Anomaly engine cycle complete: {summary['evaluated']} evaluated, "
        f"{summary['generated']} AnomalyEvent(s) generated"
    )
    return summary


def run_anomaly_engine_loop(
    session_factory: Callable[[], Any],
    redis_client: Any,
    broadcast_fn: Callable[[Any, AnomalyEvent], None],
    interval_seconds: float = 60.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Runs run_anomaly_engine_cycle on a fixed interval, forever.

    Intended as the target of a single daemon thread started from app.py's
    lifespan, mirroring analytics.signal_combiner.run_signal_combiner_loop.
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_anomaly_engine_cycle(session_factory, redis_client, broadcast_fn)
        except Exception as e:
            logger.error(f"Anomaly engine: cycle failed, will retry next interval: {e}")
        stop_event.wait(interval_seconds)
