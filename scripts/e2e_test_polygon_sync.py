"""Bounded, real end-to-end test of PolygonSyncService against a real RPC
and the DB_SESSION_FACTORY/REDIS_URL this environment is configured with
(confirmed by the user to be a dev/test instance, not production). NOT
part of the pytest suite -- run directly:

    python3 scripts/e2e_test_polygon_sync.py [--duration 45]

Unlike scripts/smoke_test_polygon_rpc.py (read-only decode-only check),
this exercises the actual write path: PolygonSyncService.start(), real
OnchainTrade/WalletProfile writes, real Redis polygon:last_block, then
.stop() after --duration seconds. Never prints the RPC URL or lets a raw
exception traceback escape.
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[1]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from dotenv import load_dotenv

from blockchain.log_sanitizer import redact_urls

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("e2e_test_polygon_sync")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=45, help="Seconds to let the service run")
    args = parser.parse_args()

    rpc_url = os.getenv("POLYGON_RPC_URL")
    if not rpc_url:
        logger.error("POLYGON_RPC_URL is not set in the environment")
        return 1
    logger.info(f"Using RPC endpoint: {rpc_url[:20]}... (truncated, not logging the key)")

    from db import SessionLocal, engine, ensure_additive_columns
    from redis_config import r as redis_client
    from blockchain.polygon_sync import PolygonSyncService
    from models.orm import OnchainTrade, WalletProfile, PolygonSyncState, Base

    Base.metadata.create_all(bind=engine)  # matches app.py lifespan's own startup call
    ensure_additive_columns()

    test_start_time = time.time()

    service = PolygonSyncService(rpc_url=rpc_url, db_session_factory=SessionLocal, redis_client=redis_client)

    logger.info(f"Starting PolygonSyncService for {args.duration}s...")
    service.start()

    try:
        time.sleep(args.duration)
    finally:
        logger.info("Stopping PolygonSyncService...")
        service.stop()

    logger.info("Service metrics after run:")
    for key, value in service.metrics.items():
        logger.info(f"  {key}: {value}")

    db = SessionLocal()
    try:
        new_trades = (
            db.query(OnchainTrade)
            .filter(OnchainTrade.block_timestamp >= test_start_time - 60)
            .order_by(OnchainTrade.block_timestamp.desc())
            .limit(10)
            .all()
        )
        updated_profiles = (
            db.query(WalletProfile)
            .filter(WalletProfile.last_updated >= test_start_time)
            .all()
        )
        sync_state = db.query(PolygonSyncState).order_by(PolygonSyncState.id.desc()).first()

        logger.info(f"OnchainTrade rows written this run (sample of up to 10): {len(new_trades)}")
        for trade in new_trades[:5]:
            logger.info(
                f"  blockchain_id={trade.blockchain_id} wallet={trade.wallet_address} "
                f"market_id={trade.market_id} usd_volume={trade.usd_volume:.4f} "
                f"entry_price={trade.entry_price:.6f}"
            )

        logger.info(f"WalletProfile rows updated this run: {len(updated_profiles)}")
        for profile in updated_profiles[:5]:
            logger.info(
                f"  wallet={profile.wallet_address} total_trades={profile.total_trades} "
                f"score_stale={profile.score_stale}"
            )

        if sync_state is not None:
            logger.info(
                f"Latest PolygonSyncState: last_block={sync_state.last_block} "
                f"events_processed={sync_state.events_processed}"
            )
        else:
            logger.warning("No PolygonSyncState row found -- service may not have completed a full cycle")
    finally:
        db.close()

    try:
        last_block_redis = redis_client.get("polygon:last_block")
        logger.info(f"Redis polygon:last_block = {last_block_redis}")
    except Exception as e:
        logger.warning(f"Could not read polygon:last_block from Redis: {redact_urls(e)}")

    if service.metrics["events_processed_total"] == 0 and service.metrics["rpc_errors_total"] > 0:
        logger.error("No events processed and RPC errors occurred -- investigate before trusting this run.")
        return 1

    logger.info("End-to-end test complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.error(f"E2E test failed with an unexpected {type(e).__name__}: {redact_urls(e)}")
        sys.exit(1)
