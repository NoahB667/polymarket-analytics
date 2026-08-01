"""I/O orchestration layer for wallet intelligence: reads OnchainTrade rows,
scores wallets via analytics.wallet_intelligence, and persists results to
PostgreSQL (WalletProfile) and Redis (hot cache). Produces per-market
Signal2Score aggregates.

Not on the WebSocket hot path — Dune-sourced data is minutes-to-hours behind
live trading, so this runs as a batch/retroactive job.
"""

import logging
import time
from typing import Any, Dict, List

import orjson

from models.orm import OnchainTrade, WalletProfile as WalletProfileORM
from models.dataclasses import Signal2Score, WalletProfile as WalletProfileDTO
from analytics.wallet_intelligence import compile_profile, calculate_insider_score

logger = logging.getLogger("polymarket.blockchain.wallet_profiler")

WALLET_CACHE_TTL_SECS = 3600
MARKET_RISK_CACHE_TTL_SECS = 300
HIGH_INSIDER_SCORE_THRESHOLD = 0.6
SIGNAL2_SAMPLE_SIZE_NORMALIZER = 50


def _row_to_trade_dict(row: OnchainTrade) -> Dict[str, Any]:
    return {
        "market_id": row.market_id,
        "category": row.category,
        "entry_price": row.entry_price,
        "outcome": row.outcome,
        "resolved_outcome": row.resolved_outcome,
        "usd_volume": row.usd_volume,
        "block_timestamp": row.block_timestamp,
        "market_end_time": row.market_end_time,
    }


def profile_wallet(db: Any, wallet_address: str, redis_client: Any) -> WalletProfileDTO:
    """Reads a wallet's on-chain trades, scores it, and persists the result.

    Args:
        db: SQLAlchemy session.
        wallet_address: Wallet to profile.
        redis_client: Redis client (matches the redis_config.r interface).

    Returns:
        The scored models.dataclasses.WalletProfile.
    """
    rows = db.query(OnchainTrade).filter(OnchainTrade.wallet_address == wallet_address).all()
    trades = [_row_to_trade_dict(r) for r in rows]

    profile = compile_profile(wallet_address, trades)
    calculate_insider_score(profile)

    try:
        _upsert_wallet_profile(db, profile)
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist WalletProfile for {wallet_address}: {e}")

    try:
        _cache_wallet_profile(redis_client, profile)
    except Exception as e:
        logger.error(f"Failed to cache WalletProfile for {wallet_address}: {e}")

    return profile


def _upsert_wallet_profile(db: Any, profile: WalletProfileDTO) -> None:
    account_age_days = max(
        0.0, (time.time() - profile.first_trade_date.timestamp()) / 86400.0
    )
    db.merge(
        WalletProfileORM(
            wallet_address=profile.wallet_address,
            total_trades=profile.total_trades,
            distinct_markets=profile.unique_markets,
            long_shot_attempts=profile.longshot_attempts,
            long_shot_wins=profile.longshot_wins,
            long_shot_win_rate=profile.longshot_win_rate,
            category_concentration=profile.category_concentration,
            account_age_days=round(account_age_days, 2),
            average_position_size=profile.avg_bet_size,
            avg_implied_prob_at_entry=profile.avg_implied_prob_at_entry,
            avg_days_before_resolution=profile.avg_days_before_resolution,
            new_account_flag=profile.new_account_flag,
            top_categories=",".join(profile.top_categories),
            insider_score=profile.insider_score,
            last_updated=time.time(),
        )
    )
    db.commit()


def _cache_wallet_profile(redis_client: Any, profile: WalletProfileDTO) -> None:
    payload = {
        "wallet_address": profile.wallet_address,
        "total_trades": profile.total_trades,
        "unique_markets": profile.unique_markets,
        "longshot_win_rate": profile.longshot_win_rate,
        "category_concentration": profile.category_concentration,
        "new_account_flag": profile.new_account_flag,
        "avg_bet_size": profile.avg_bet_size,
        "insider_score": profile.insider_score,
        "score_components": profile.score_components,
    }
    redis_client.setex(
        f"wallet:{profile.wallet_address}", WALLET_CACHE_TTL_SECS, orjson.dumps(payload)
    )


def profile_all_wallets(db: Any, redis_client: Any) -> List[Any]:
    """Batch-profiles every distinct wallet present in OnchainTrade.

    Best-effort per wallet: a failure profiling one wallet is logged and
    skipped, it does not stop the batch.

    Args:
        db: SQLAlchemy session.
        redis_client: Redis client (matches the redis_config.r interface).

    Returns:
        List of scored WalletProfile DTOs, one per successfully profiled wallet.
    """
    wallet_addresses = [row[0] for row in db.query(OnchainTrade.wallet_address).distinct().all()]
    profiles = []
    for address in wallet_addresses:
        try:
            profiles.append(profile_wallet(db, address, redis_client))
        except Exception as e:
            logger.error(f"Skipping wallet {address} after profiling failure: {e}")
    return profiles


def market_insider_risk(db: Any, market_id: str, redis_client: Any) -> float:
    """Fraction of a market's on-chain volume that came from high insider-score wallets.

    Caches the result at market:insider_risk:{market_id} for MARKET_RISK_CACHE_TTL_SECS.

    Args:
        db: SQLAlchemy session.
        market_id: Market to evaluate.
        redis_client: Redis client (matches the redis_config.r interface).

    Returns:
        Fraction (0.0-1.0) of the market's on-chain volume attributable to
        wallets whose insider_score exceeds HIGH_INSIDER_SCORE_THRESHOLD.
    """
    cache_key = f"market:insider_risk:{market_id}"
    try:
        cached = redis_client.get(cache_key)
        if cached is not None:
            return float(cached)
    except Exception as e:
        logger.warning(f"Redis read failed for {cache_key}, recomputing: {e}")

    rows = db.query(OnchainTrade).filter(OnchainTrade.market_id == market_id).all()
    if not rows:
        return 0.0

    wallet_addresses = set(row.wallet_address for row in rows)
    insider_scores_by_wallet: Dict[str, float] = {}
    for address in wallet_addresses:
        wallet_profile = db.query(WalletProfileORM).filter_by(wallet_address=address).first()
        insider_scores_by_wallet[address] = wallet_profile.insider_score if wallet_profile else 0.0

    suspicious_volume = 0.0
    total_volume = 0.0
    for row in rows:
        total_volume += row.usd_volume
        if insider_scores_by_wallet[row.wallet_address] > HIGH_INSIDER_SCORE_THRESHOLD:
            suspicious_volume += row.usd_volume

    risk = suspicious_volume / total_volume if total_volume > 0 else 0.0

    try:
        redis_client.setex(cache_key, MARKET_RISK_CACHE_TTL_SECS, risk)
    except Exception as e:
        logger.warning(f"Redis write failed for {cache_key}: {e}")

    return risk


def build_signal2_score(db: Any, market_id: str, redis_client: Any) -> Signal2Score:
    """Assembles a Signal2Score for a market from its active wallets' insider scores.

    Args:
        db: SQLAlchemy session.
        market_id: Market to evaluate.
        redis_client: Redis client (matches the redis_config.r interface).

    Returns:
        Signal2Score dataclass aggregating market_insider_risk, average
        insider score, high-score wallet count, and sample-size confidence.
    """
    risk = market_insider_risk(db, market_id, redis_client)

    rows = db.query(OnchainTrade).filter(OnchainTrade.market_id == market_id).all()
    wallet_addresses = set(row.wallet_address for row in rows)

    insider_scores = []
    for address in wallet_addresses:
        wallet_profile = db.query(WalletProfileORM).filter_by(wallet_address=address).first()
        if wallet_profile is not None:
            insider_scores.append(wallet_profile.insider_score)

    sample_size = len(insider_scores)
    high_score_wallet_count = sum(1 for s in insider_scores if s > HIGH_INSIDER_SCORE_THRESHOLD)
    avg_insider_score = sum(insider_scores) / sample_size if sample_size > 0 else 0.0
    confidence = min(sample_size / SIGNAL2_SAMPLE_SIZE_NORMALIZER, 1.0) * risk

    return Signal2Score(
        market_id=market_id,
        timestamp=time.time(),
        market_insider_risk=round(risk, 4),
        high_score_wallet_count=high_score_wallet_count,
        avg_insider_score=round(avg_insider_score, 4),
        sample_size=sample_size,
        confidence=round(confidence, 4),
    )
