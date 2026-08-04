"""Daily scheduler that re-queries Dune for on-chain trades on currently
auto-tracked markets and refreshes wallet profiles from the results.

Unlike scripts/backfill_polygon.py (a one-off, single-topic manual script
hardcoded to a "fed rate cut 2026" keyword filter), this module is
market-aware: it queries whatever is currently active in AutoSubscription,
so newly auto-discovered markets automatically get on-chain wallet
intelligence without a manual re-run.

WALLET_INTELLIGENCE_ENABLED defaults to false. Dune SQL executions consume
real query credits/rate-limit budget against the account's plan, so this
must be explicitly enabled -- after reviewing what build_wallet_intelligence_query
actually produces -- rather than running the moment this module is wired
into app.py's lifespan.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from blockchain.dune_client import DuneClient
from blockchain.market_resolution_client import MarketResolution, MarketResolutionClient
from blockchain.wallet_profiler import profile_all_wallets, profile_wallet
from signal_core.wallet_intelligence import classify_category
from db import SessionLocal
from models.orm import AutoSubscription, OnchainTrade, WalletProfile

logger = logging.getLogger("polymarket.core.wallet_intelligence_scheduler")

WALLET_INTELLIGENCE_ENABLED = os.getenv("WALLET_INTELLIGENCE_ENABLED", "false").lower() == "true"
WALLET_INTELLIGENCE_INTERVAL_HOURS = float(os.getenv("WALLET_INTELLIGENCE_INTERVAL_HOURS", "24"))
WALLET_INTELLIGENCE_LOOKBACK_DAYS = int(os.getenv("WALLET_INTELLIGENCE_LOOKBACK_DAYS", "2"))
WALLET_INTELLIGENCE_MIN_USD = float(os.getenv("WALLET_INTELLIGENCE_MIN_USD", "100"))
WALLET_INTELLIGENCE_ROW_LIMIT = int(os.getenv("WALLET_INTELLIGENCE_ROW_LIMIT", "10000"))
# Verified live against Dune: polymarket_polygon.market_trades.action currently
# holds 'CLOB trade' (3.19M rows/day), not the lowercase 'clob' that both this
# query and the pre-existing scripts/backfill_polygon.py assumed -- that
# stale filter was silently zeroing out every result (schema drift in Dune's
# spellbook table since backfill_polygon.py was last written/run).
CLOB_ACTION_VALUE = "CLOB trade"

CONDITION_ID_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")

SCORE_RECALCULATION_INTERVAL_HOURS = float(os.getenv("SCORE_RECALCULATION_INTERVAL_HOURS", "1"))


def get_active_condition_ids(session_factory=SessionLocal) -> List[str]:
    """Returns distinct condition_ids for all currently active auto-tracked markets."""
    db = session_factory()
    try:
        rows = (
            db.query(AutoSubscription.condition_id)
            .filter(AutoSubscription.status == "active")
            .filter(AutoSubscription.condition_id.isnot(None))
            .distinct()
            .all()
        )
        return [r[0] for r in rows if r[0]]
    finally:
        db.close()


def _sanitize_condition_ids(condition_ids: List[str]) -> List[str]:
    """Drops anything that isn't a well-formed 0x-hex string before it reaches raw SQL."""
    valid = [cid for cid in condition_ids if cid and CONDITION_ID_PATTERN.match(cid)]
    dropped = len(condition_ids) - len(valid)
    if dropped:
        logger.warning(f"Wallet intelligence: dropped {dropped} malformed condition_id(s)")
    return valid


def build_wallet_intelligence_query(
    condition_ids: List[str],
    lookback_days: int = WALLET_INTELLIGENCE_LOOKBACK_DAYS,
    min_usd: float = WALLET_INTELLIGENCE_MIN_USD,
    row_limit: int = WALLET_INTELLIGENCE_ROW_LIMIT,
) -> Optional[str]:
    """Builds the Dune SQL pulling on-chain trades for currently auto-tracked markets.

    Mirrors scripts/backfill_polygon.py's maker/taker de-dup and action='clob'
    filtering rationale (see reference/polymarket_architecture.md) -- maker
    (not taker) is the wallet that posted the order, is_taker_side=TRUE
    de-dupes the two OrderFilled events every CLOB match emits, and
    action='clob' excludes AMM trades. The difference from that script is
    the WHERE clause: filters by the live set of auto-tracked condition_ids
    instead of a hardcoded event-name keyword, and scopes to a rolling
    lookback window rather than all history since 2025-01-01 -- overlapping
    windows across daily runs are safe since ingestion dedupes by
    blockchain_id.

    Args:
        condition_ids: On-chain condition_ids (0x-prefixed hex) of markets
            to query on-chain trade data for.
        lookback_days: Only trades from this many days back are considered.
        min_usd: Minimum trade size in USD (filters dust).
        row_limit: Hard cap on rows returned by this query.

    Returns:
        The SQL string, or None if condition_ids is empty after sanitizing
        -- callers should skip execution entirely rather than run an
        unfiltered query against Dune.
    """
    sanitized = _sanitize_condition_ids(condition_ids)
    if not sanitized:
        return None

    # Verified live against Dune: to_hex(varbinary) returns UPPERCASE hex,
    # while Gamma's conditionId (what we store) is lowercase -- a naive
    # comparison silently matches zero rows. Lowercasing both sides here
    # makes the comparison case-insensitive regardless of which casing
    # either side happens to use.
    id_list = ", ".join(f"'{cid.lower()}'" for cid in sanitized)
    since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    return (
        "SELECT\n"
        "    upper(to_hex(t.tx_hash)) || '-' || CAST(t.evt_index AS VARCHAR) as blockchain_id,\n"
        "    t.maker as wallet_address,\n"
        "    to_hex(t.condition_id) as market_id,\n"
        "    t.event_market_name as event_market_name,\n"
        "    t.question,\n"
        "    t.token_outcome as outcome,\n"
        "    t.amount as usd_volume,\n"
        "    t.price as entry_price,\n"
        "    to_unixtime(t.block_time) as block_timestamp\n"
        "FROM polymarket_polygon.market_trades t\n"
        f"WHERE t.block_time >= CAST('{since_date}' AS TIMESTAMP)\n"
        "  AND t.is_taker_side = TRUE\n"
        f"  AND t.action = '{CLOB_ACTION_VALUE}'\n"
        "  AND t.maker IS NOT NULL\n"
        f"  AND t.amount >= {min_usd}\n"
        f"  AND ('0x' || lower(to_hex(t.condition_id))) IN ({id_list})\n"
        "ORDER BY t.block_time ASC\n"
        f"LIMIT {row_limit}"
    )


def _ingest_rows(
    db: Any,
    dune: DuneClient,
    execution_id: str,
    resolution_client: MarketResolutionClient,
) -> int:
    """Ingests Dune result rows into OnchainTrade, deduping by blockchain_id.

    Mirrors scripts/backfill_polygon.py's per-row ingestion loop: best-effort,
    one row's failure is logged and skipped rather than aborting the batch.

    Returns:
        Count of newly-inserted rows (pre-existing blockchain_ids don't count).
    """
    ingested = 0
    for row in dune.fetch_results_paginated(execution_id):
        blockchain_id = row.get("blockchain_id")
        try:
            existing = db.query(OnchainTrade).filter_by(blockchain_id=blockchain_id).first()
            if existing is not None:
                # Step 9's live monitor may have inserted this row first with
                # category/question/resolved_outcome left NULL (it has no
                # tokenId -> market metadata resolver) -- backfill those
                # fields now rather than permanently skipping them. A row
                # that already has a category came from a prior Dune
                # ingestion and is left untouched.
                if existing.category is None:
                    market_id = row.get("market_id")
                    resolution = (
                        resolution_client.resolve_market(market_id)
                        if market_id
                        else MarketResolution(resolved_outcome=None, market_end_time=None)
                    )
                    existing.question = existing.question or row.get("question")
                    existing.outcome = existing.outcome or row.get("outcome")
                    existing.category = classify_category(row.get("event_market_name", ""))
                    existing.resolved_outcome = existing.resolved_outcome or resolution.resolved_outcome
                    existing.market_end_time = existing.market_end_time or resolution.market_end_time
                    db.commit()
                continue

            market_id = row.get("market_id")
            resolution = (
                resolution_client.resolve_market(market_id)
                if market_id
                else MarketResolution(resolved_outcome=None, market_end_time=None)
            )

            db.add(OnchainTrade(
                blockchain_id=blockchain_id,
                wallet_address=row.get("wallet_address"),
                market_id=market_id,
                question=row.get("question"),
                outcome=row.get("outcome", "unknown"),
                category=classify_category(row.get("event_market_name", "")),
                usd_volume=float(row.get("usd_volume", 0.0)),
                entry_price=float(row.get("entry_price", 0.0)),
                resolved_outcome=resolution.resolved_outcome,
                market_end_time=resolution.market_end_time,
                block_timestamp=float(row.get("block_timestamp", time.time())),
            ))
            db.commit()
            ingested += 1
        except Exception as e:
            db.rollback()
            logger.error(f"Wallet intelligence: skipping row {blockchain_id} after ingestion failure: {e}")
            continue
    return ingested


def run_wallet_intelligence_cycle(
    redis_client: Any = None,
    session_factory=SessionLocal,
    dune_client: Optional[DuneClient] = None,
) -> dict:
    """Runs one full cycle: query Dune for active markets' on-chain trades,
    ingest new rows, then refresh wallet profiles.

    Best-effort at every stage -- never raises. Returns a summary dict even
    on early exit (zeroed counters) so the caller can log it either way.

    Args:
        redis_client: Passed through to profile_all_wallets for hot-cache
            writes; if None, the profiling step is skipped entirely (there's
            nothing useful to do with newly-ingested trades without it).
        session_factory: DB session factory; defaults to the app's real
            SessionLocal, overridable in tests.
        dune_client: Optional pre-built DuneClient (for tests); defaults to
            constructing a real one, which requires DUNE_API_KEY.

    Returns:
        {"condition_ids": int, "ingested": int, "profiled": int}
    """
    summary = {"condition_ids": 0, "ingested": 0, "profiled": 0}

    condition_ids = get_active_condition_ids(session_factory)
    summary["condition_ids"] = len(condition_ids)
    if not condition_ids:
        logger.info("Wallet intelligence: no active auto-tracked markets with a condition_id, skipping cycle")
        return summary

    sql = build_wallet_intelligence_query(condition_ids)
    if sql is None:
        logger.warning("Wallet intelligence: no valid condition_ids after sanitization, skipping cycle")
        return summary

    try:
        dune = dune_client or DuneClient()
        execution_id = dune.execute_raw_sql(sql)
        if not dune.poll_execution_status(execution_id):
            logger.error("Wallet intelligence: Dune execution failed or timed out")
            return summary
    except Exception as e:
        logger.error(f"Wallet intelligence: Dune pipeline failure: {e}")
        return summary

    db = session_factory()
    try:
        resolution_client = MarketResolutionClient()
        summary["ingested"] = _ingest_rows(db, dune, execution_id, resolution_client)
    finally:
        db.close()

    if summary["ingested"] > 0 and redis_client is not None:
        db = session_factory()
        try:
            profiles = profile_all_wallets(db, redis_client)
            summary["profiled"] = len(profiles)
        finally:
            db.close()

    logger.info(
        f"Wallet intelligence cycle complete: {summary['condition_ids']} tracked markets, "
        f"+{summary['ingested']} new on-chain trades, {summary['profiled']} wallets profiled"
    )
    return summary


def run_wallet_intelligence_loop(
    redis_client: Any = None,
    session_factory=SessionLocal,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Runs run_wallet_intelligence_cycle on a daily interval, forever.

    Intended as the target of a single daemon thread started from app.py's
    lifespan, mirroring core.auto_discovery.run_scheduler_loop's shape.
    Never raises -- any cycle failure is logged and the loop continues.
    """
    if not WALLET_INTELLIGENCE_ENABLED:
        logger.info("Wallet intelligence scheduler disabled via WALLET_INTELLIGENCE_ENABLED=false")
        return

    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_wallet_intelligence_cycle(redis_client=redis_client, session_factory=session_factory)
        except Exception as e:
            logger.error(f"Wallet intelligence: cycle failed, will retry next interval: {e}")
        stop_event.wait(WALLET_INTELLIGENCE_INTERVAL_HOURS * 3600)


def run_score_recalculation_cycle(redis_client: Any = None, session_factory=SessionLocal) -> dict:
    """Recomputes insider_score for every wallet flagged score_stale by
    PolygonSyncService's hot-path counter increments (Step 9). Keeps the
    live sync loop cheap by deferring the expensive full recompute here.

    Best-effort per wallet: a failure is logged and skipped, not fatal to
    the cycle.

    Returns:
        {"stale_wallets": int, "recalculated": int}
    """
    summary = {"stale_wallets": 0, "recalculated": 0}
    db = session_factory()
    try:
        stale_addresses = [
            row[0] for row in db.query(WalletProfile.wallet_address)
            .filter(WalletProfile.score_stale.is_(True)).all()
        ]
        summary["stale_wallets"] = len(stale_addresses)
        for address in stale_addresses:
            try:
                profile_wallet(db, address, redis_client)
                stale_row = db.query(WalletProfile).filter_by(wallet_address=address).first()
                if stale_row is not None:
                    stale_row.score_stale = False
                    db.commit()
                summary["recalculated"] += 1
            except Exception as e:
                db.rollback()
                logger.error(f"Score recalculation: skipping {address} after failure: {e}")
    finally:
        db.close()

    logger.info(f"Score recalculation cycle complete: {summary['recalculated']}/{summary['stale_wallets']} wallets")
    return summary


def run_score_recalculation_loop(
    redis_client: Any = None,
    session_factory=SessionLocal,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Runs run_score_recalculation_cycle hourly, forever. Mirrors
    run_wallet_intelligence_loop's shape -- single daemon thread from
    app.py's lifespan, never raises out of the loop.
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_score_recalculation_cycle(redis_client=redis_client, session_factory=session_factory)
        except Exception as e:
            logger.error(f"Score recalculation: cycle failed, will retry next interval: {e}")
        stop_event.wait(SCORE_RECALCULATION_INTERVAL_HOURS * 3600)
