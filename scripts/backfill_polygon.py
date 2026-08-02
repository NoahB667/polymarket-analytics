import os
import sys
import time
import logging
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal, engine
from models.orm import OnchainTrade, Base
from blockchain.dune_client import DuneClient
from blockchain.market_resolution_client import MarketResolution, MarketResolutionClient
from signal_core.wallet_intelligence import classify_category

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("backfill_polygon")


def run_backfill() -> None:
    logger.info("Initializing raw historical Polygon on-chain backfill...")

    dune = DuneClient()
    target_event_keywords = "%fed%rate%cut%2026%"

    # maker (not taker) is the wallet that posted the order — the taker leg is
    # often an automated Polymarket contract. is_taker_side=TRUE de-dupes the
    # two OrderFilled events every CLOB match emits. action='clob' excludes
    # AMM trades. See reference/polymarket_architecture.md.
    optimized_sql = f"""
    SELECT
        upper(to_hex(t.tx_hash)) || '-' || CAST(t.evt_index AS VARCHAR) as blockchain_id,
        t.maker as wallet_address,
        to_hex(t.condition_id) as market_id,
        t.event_market_name as event_market_name,
        t.question,
        t.token_outcome as outcome,
        t.amount as usd_volume,
        t.price as entry_price,
        to_unixtime(t.block_time) as block_timestamp
    FROM polymarket_polygon.market_trades t
    WHERE t.block_time >= CAST('2025-01-01' AS TIMESTAMP)
      AND t.is_taker_side = TRUE
      AND t.action = 'clob'
      AND t.maker IS NOT NULL
      AND t.amount >= 100
      AND lower(t.event_market_name) LIKE '{target_event_keywords}'
    ORDER BY t.block_time ASC
    LIMIT 50
    """

    try:
        execution_id = dune.execute_raw_sql(optimized_sql)
        if not dune.poll_execution_status(execution_id):
            logger.error("Dune execution failed.")
            return
    except Exception as e:
        logger.critical(f"Dune pipeline failure: {e}")
        return

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    resolution_client = MarketResolutionClient()

    batch_counter = 0
    try:
        for row in dune.fetch_results_paginated(execution_id):
            blockchain_id = row.get("blockchain_id")

            try:
                if (
                    db.query(OnchainTrade)
                    .filter_by(blockchain_id=blockchain_id)
                    .first()
                ):
                    continue

                market_id = row.get("market_id")
                resolution = (
                    resolution_client.resolve_market(market_id)
                    if market_id
                    else MarketResolution(resolved_outcome=None, market_end_time=None)
                )

                raw_trade = OnchainTrade(
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
                )
                db.add(raw_trade)
                db.commit()
                batch_counter += 1
            except Exception as row_error:
                db.rollback()
                logger.error(
                    f"Skipping row {blockchain_id} after ingestion failure: {row_error}"
                )
                continue

        logger.info(f"Backfill complete! Committed {batch_counter} new trades.")

    except Exception as e:
        logger.error(f"Backfill pipeline failure: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    run_backfill()
