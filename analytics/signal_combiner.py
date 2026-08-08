"""I/O orchestration for Step 10: reads cached Signal 1 (Redis) and Signal 2
(via blockchain.wallet_profiler.build_signal2_score) for every active
auto-tracked market, combines them via signal_core.signal_combiner, persists
the append-only Signal log, and reports which markets produced a TRADE
decision.

Not on the WebSocket hot path -- runs on its own polling thread (started
from app.py's lifespan), since this does DB reads/writes on every cycle
that must never happen inside on_trade_callback (CLAUDE.md: "Never Block
the Hot Path").
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

import orjson

from blockchain.wallet_profiler import build_signal2_score
from models.dataclasses import CombinedSignal
from models.orm import AutoSubscription, Signal
from signal_core.signal_combiner import (
    ACTION_IGNORE,
    ACTION_TRADE,
    ACTION_WATCH,
    check_gates,
    classify_action,
    combine_confidence,
)

logger = logging.getLogger("polymarket.analytics.signal_combiner")


def read_signal1(redis_client: Any, slug: str) -> Optional[dict]:
    """Reads and decodes the cached Signal 1 payload for a market.

    Args:
        redis_client: Redis client (matches the redis_config.r interface).
        slug: The market's human-readable slug (Signal 1's cache key space).

    Returns:
        The decoded dict from analytics.order_flow.generate_signal_score(),
        or None if nothing is cached (TTL expired or never scored).
    """
    raw = redis_client.get(f"signal:1:score:{slug}")
    if raw is None:
        return None
    return orjson.loads(raw)


def build_combined_signal(
    db: Any,
    redis_client: Any,
    auto_subscription: AutoSubscription,
    has_open_position: bool,
) -> Optional[CombinedSignal]:
    """Combines a single market's Signal 1 + Signal 2 into a CombinedSignal.

    Args:
        db: SQLAlchemy session.
        redis_client: Redis client (matches the redis_config.r interface).
        auto_subscription: The market's AutoSubscription row (source of
            slug<->condition_id mapping and volume_24h for the liquidity gate).
        has_open_position: Whether a paper position is already open in this market.

    Returns:
        A CombinedSignal, or None if Signal 1 isn't cached or the market has
        no condition_id (Signal 2 requires it).
    """
    signal1 = read_signal1(redis_client, auto_subscription.slug)
    if signal1 is None or not auto_subscription.condition_id:
        return None

    signal2 = build_signal2_score(db, auto_subscription.condition_id, redis_client)

    combined_score = combine_confidence(signal1["confidence"], signal2.confidence)
    gates_passed = check_gates(
        signal1_confidence=signal1["confidence"],
        signal2_market_insider_risk=signal2.market_insider_risk,
        market_volume_usd=auto_subscription.volume_24h or 0.0,
        latest_price=signal1.get("latest_price", 0.0),
        has_open_position=has_open_position,
    )
    action = classify_action(combined_score, gates_passed)

    return CombinedSignal(
        market_id=auto_subscription.condition_id,
        slug=auto_subscription.slug,
        direction=signal1["direction"],
        combined_score=combined_score,
        signal1_confidence=signal1["confidence"],
        signal2_confidence=signal2.confidence,
        signal2_market_insider_risk=signal2.market_insider_risk,
        recommended_action=action,
        gates_passed=gates_passed,
        timestamp=time.time(),
    )


def persist_signal(db: Any, combined: CombinedSignal) -> None:
    """Appends a CombinedSignal to the append-only `signal` table.

    Best-effort: a failure is logged, never raised (CLAUDE.md rule 3).

    Args:
        db: SQLAlchemy session.
        combined: The CombinedSignal to persist.
    """
    try:
        db.add(Signal(
            market_id=combined.market_id,
            slug=combined.slug,
            timestamp=combined.timestamp,
            direction=combined.direction,
            signal1_confidence=combined.signal1_confidence,
            signal2_confidence=combined.signal2_confidence,
            signal2_market_insider_risk=combined.signal2_market_insider_risk,
            combined_score=combined.combined_score,
            recommended_action=combined.recommended_action,
            gates_passed=combined.gates_passed,
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Signal combiner: failed to persist signal for {combined.slug}: {e}")


def run_signal_combiner_cycle(
    session_factory: Callable[[], Any],
    redis_client: Any,
    open_position_fn: Callable[[Any, str], bool],
    on_trade_signal: Optional[Callable[[Any, CombinedSignal], None]] = None,
) -> dict:
    """Runs one full cycle: evaluate every active auto-tracked market.

    Best-effort per market: a failure evaluating one market is logged and
    skipped, it does not stop the cycle.

    Args:
        session_factory: DB session factory.
        redis_client: Redis client (matches the redis_config.r interface).
        open_position_fn: called as open_position_fn(db, market_id) -> bool,
            checks whether a paper position is already open in that market.
        on_trade_signal: optional callback invoked as
            on_trade_signal(db, combined) for every CombinedSignal whose
            recommended_action is TRADE -- wired to the paper trader's
            open_position in Task 8.

    Returns:
        {"evaluated": int, "trade_signals": int}
    """
    summary = {"evaluated": 0, "trade_signals": 0}
    db = session_factory()
    try:
        active_rows = db.query(AutoSubscription).filter_by(status="active").all()
        for row in active_rows:
            if not row.condition_id:
                continue
            try:
                has_position = open_position_fn(db, row.condition_id)
                combined = build_combined_signal(db, redis_client, row, has_position)
                if combined is None:
                    continue
                summary["evaluated"] += 1
                persist_signal(db, combined)
                if combined.recommended_action == ACTION_TRADE:
                    summary["trade_signals"] += 1
                    if on_trade_signal is not None:
                        on_trade_signal(db, combined)
            except Exception as e:
                logger.error(f"Signal combiner: failed to evaluate {row.slug}: {e}")
                continue
    finally:
        db.close()

    logger.info(
        f"Signal combiner cycle complete: {summary['evaluated']} evaluated, "
        f"{summary['trade_signals']} TRADE signal(s)"
    )
    return summary


def run_signal_combiner_loop(
    session_factory: Callable[[], Any],
    redis_client: Any,
    open_position_fn: Callable[[Any, str], bool],
    on_trade_signal: Optional[Callable[[Any, CombinedSignal], None]] = None,
    interval_seconds: float = 60.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Runs run_signal_combiner_cycle on a fixed interval, forever.

    Intended as the target of a single daemon thread started from app.py's
    lifespan, mirroring core.wallet_intelligence_scheduler's loop shape.
    Never raises -- any cycle failure is logged and the loop continues.
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_signal_combiner_cycle(session_factory, redis_client, open_position_fn, on_trade_signal)
        except Exception as e:
            logger.error(f"Signal combiner: cycle failed, will retry next interval: {e}")
        stop_event.wait(interval_seconds)
