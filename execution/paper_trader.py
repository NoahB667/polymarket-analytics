"""I/O orchestration for the paper-trading loop (Step 10): opens simulated
positions on TRADE signals from analytics.signal_combiner, tracks open
PaperPosition rows, and closes them on stop-loss / take-profit / market
resolution.

Paper trading only -- never places a real order against the Polymarket CLOB
API (CLAUDE.md rule 5). Only BUY-direction signals open a position; see
docs/superpowers/plans/2026-08-07-signal-combiner-paper-trader.md "Design
Notes" item 3 for why SELL is out of scope for this iteration.
"""

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

import requests

from models.dataclasses import CombinedSignal
from models.orm import PaperPosition
from signal_core.signal_combiner import DEFAULT_ODDS_RATIO, kelly_fraction

logger = logging.getLogger("polymarket.execution.paper_trader")

PAPER_INITIAL_CAPITAL = float(os.getenv("PAPER_INITIAL_CAPITAL", "10000"))
STOP_LOSS_FRACTION = 0.50
TAKE_PROFIT_MULTIPLE = 2.00
MIN_CLOSED_TRADES_FOR_LIVE_WIN_RATE = 10
DEFAULT_WIN_RATE = 0.50
CLOB_MIDPOINT_URL = "https://clob.polymarket.com/midpoint?token_id={asset_id}"


def has_open_position(db: Any, market_id: str) -> bool:
    """Whether a paper position is currently open in the given market.

    This is the `open_position_fn` shape analytics.signal_combiner.run_signal_combiner_cycle expects.
    """
    return db.query(PaperPosition).filter_by(market_id=market_id, status="open").first() is not None


def get_available_capital(db: Any) -> float:
    """Current paper capital: initial + realized P&L - cost of open positions."""
    open_positions = db.query(PaperPosition).filter_by(status="open").all()
    committed = sum(p.cost for p in open_positions)
    closed_positions = db.query(PaperPosition).filter_by(status="closed").all()
    realized_pnl = sum(p.pnl or 0.0 for p in closed_positions)
    return PAPER_INITIAL_CAPITAL + realized_pnl - committed


def get_rolling_win_rate(db: Any) -> float:
    """Live win rate from closed positions, or DEFAULT_WIN_RATE below the minimum sample size."""
    closed_positions = db.query(PaperPosition).filter_by(status="closed").all()
    if len(closed_positions) < MIN_CLOSED_TRADES_FOR_LIVE_WIN_RATE:
        return DEFAULT_WIN_RATE
    wins = sum(1 for p in closed_positions if (p.pnl or 0.0) > 0)
    return wins / len(closed_positions)


def open_position(
    db: Any,
    combined: CombinedSignal,
    asset_id: str,
    entry_price: float,
    alert_callback: Optional[Callable[[str], None]] = None,
) -> Optional[PaperPosition]:
    """Opens a Kelly-sized paper position for a TRADE-recommended CombinedSignal.

    Only handles direction == "BUY" -- callers should not invoke this for
    SELL signals (see module docstring).

    Args:
        db: SQLAlchemy session.
        combined: The CombinedSignal that triggered this (already confirmed TRADE).
        asset_id: The CLOB token id being bought.
        entry_price: The market's current price for asset_id.
        alert_callback: Optional callable(message: str) for the Telegram open alert.

    Returns:
        The persisted PaperPosition, or None if a position is already open,
        the Kelly fraction is non-positive, or the DB write failed.
    """
    if has_open_position(db, combined.market_id):
        return None

    capital = get_available_capital(db)
    win_rate = get_rolling_win_rate(db)
    fraction = kelly_fraction(win_rate, DEFAULT_ODDS_RATIO)
    if fraction <= 0.0 or entry_price <= 0.0:
        return None

    cost = round(capital * fraction, 2)
    if cost <= 0.0:
        return None
    shares = cost / entry_price

    position = PaperPosition(
        market_id=combined.market_id,
        slug=combined.slug,
        asset_id=asset_id,
        direction=combined.direction,
        entry_price=entry_price,
        shares=shares,
        cost=cost,
        entry_time=time.time(),
        signal_score=combined.combined_score,
        stop_loss_price=round(entry_price * STOP_LOSS_FRACTION, 4),
        take_profit_price=round(entry_price * TAKE_PROFIT_MULTIPLE, 4),
        status="open",
    )
    try:
        db.add(position)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Paper trader: failed to persist new position for {combined.slug}: {e}")
        return None

    if alert_callback:
        alert_callback(
            f"\U0001F4C8 PAPER TRADE OPENED\n"
            f"Market: {combined.slug}\n"
            f"Direction: {combined.direction}\n"
            f"Entry: ${entry_price:.3f} (implied {entry_price * 100:.1f}%)\n"
            f"Size: ${cost:.2f} ({fraction * 100:.1f}% of capital)\n"
            f"Signal score: {combined.combined_score:.2f}\n"
            f"Capital remaining: ${capital - cost:.2f}"
        )
    return position


def fetch_midpoint(asset_id: str) -> Optional[float]:
    """Fetches the current CLOB midpoint price for a token id.

    Mirrors analytics.order_flow.price_impact_evaluator_worker's midpoint
    fetch pattern. Best-effort: returns None on any failure.
    """
    try:
        response = requests.get(CLOB_MIDPOINT_URL.format(asset_id=asset_id), timeout=5)
        if response.status_code != 200:
            return None
        data = response.json()
        price = data.get("mid_price") or data.get("mid")
        if not price and isinstance(data, list) and data:
            price = data[0].get("mid_price") or data[0].get("mid")
        return float(price) if price else None
    except Exception as e:
        logger.warning(f"Paper trader: midpoint fetch failed for {asset_id}: {e}")
        return None


def check_exit_conditions(position: PaperPosition, current_price: float) -> Optional[str]:
    """Checks a BUY-direction position's current price against its stop-loss/take-profit.

    Args:
        position: The open PaperPosition (direction is always "BUY" for
            positions this module opens -- see open_position's docstring).
        current_price: The asset's current CLOB midpoint.

    Returns:
        "STOP_LOSS", "TAKE_PROFIT", or None if still within range.
    """
    if current_price <= position.stop_loss_price:
        return "STOP_LOSS"
    if current_price >= position.take_profit_price:
        return "TAKE_PROFIT"
    return None


def close_position(
    db: Any,
    position: PaperPosition,
    exit_price: float,
    exit_reason: str,
    alert_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """Closes an open PaperPosition, computes P&L, and fires the close alert.

    Best-effort DB write: a failure is logged and the position is left open
    for the next cycle to retry (CLAUDE.md rule 3).
    """
    proceeds = position.shares * exit_price
    pnl = proceeds - position.cost
    return_pct = (pnl / position.cost) * 100.0 if position.cost else 0.0

    position.status = "closed"
    position.exit_price = exit_price
    position.exit_time = time.time()
    position.exit_reason = exit_reason
    position.pnl = round(pnl, 2)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Paper trader: failed to persist close for position {position.id}: {e}")
        return

    if alert_callback:
        closed_positions = db.query(PaperPosition).filter_by(status="closed").all()
        wins = sum(1 for p in closed_positions if (p.pnl or 0.0) > 0)
        win_rate = wins / len(closed_positions) if closed_positions else 0.0
        capital = get_available_capital(db)
        emoji = "✅" if pnl > 0 else "❌"
        duration_hours = (position.exit_time - position.entry_time) / 3600.0
        alert_callback(
            f"{emoji} PAPER TRADE CLOSED\n"
            f"Market: {position.slug}\n"
            f"P&L: ${pnl:+.2f} ({return_pct:+.1f}%)\n"
            f"Exit reason: {exit_reason}\n"
            f"Duration: {duration_hours:.1f} hours\n"
            f"Running total: ${capital:.2f}\n"
            f"Win rate: {win_rate:.1%} ({len(closed_positions)} trades)"
        )


def run_position_monitor_cycle(session_factory: Callable[[], Any], alert_callback: Optional[Callable[[str], None]] = None) -> dict:
    """Scans every open PaperPosition and closes any that hit stop-loss/take-profit.

    Best-effort per position: a midpoint fetch failure is logged and
    skipped, retried next cycle.

    Returns:
        {"checked": int, "closed": int}
    """
    db = session_factory()
    try:
        open_positions = db.query(PaperPosition).filter_by(status="open").all()
        checked = 0
        closed = 0
        for position in open_positions:
            checked += 1
            price = fetch_midpoint(position.asset_id)
            if price is None:
                continue
            reason = check_exit_conditions(position, price)
            if reason:
                close_position(db, position, price, reason, alert_callback)
                closed += 1
        return {"checked": checked, "closed": closed}
    finally:
        db.close()


def run_position_monitor_loop(
    session_factory: Callable[[], Any],
    alert_callback: Optional[Callable[[str], None]] = None,
    interval_seconds: float = 60.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Runs run_position_monitor_cycle on a fixed interval, forever.

    Intended as the target of a single daemon thread started from app.py's
    lifespan. Never raises -- any cycle failure is logged and the loop continues.
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_position_monitor_cycle(session_factory, alert_callback)
        except Exception as e:
            logger.error(f"Paper trader: position monitor cycle failed, will retry next interval: {e}")
        stop_event.wait(interval_seconds)
