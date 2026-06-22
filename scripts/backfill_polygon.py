import os
import sys
import time
import logging
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal, engine
from models.orm import OnchainTrade, Base
from blockchain.dune_client import DuneClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_polygon")

def run_backfill():
    logger.info("Initializing raw historical Polygon on-chain backfill...")
    
    dune = DuneClient()
    target_event_keywords = "%fed%rate%cut%2026%"

    optimized_sql = f"""
    SELECT 
        upper(to_hex(t.tx_hash)) || '-' || CAST(t.evt_index AS VARCHAR) as blockchain_id,
        t.taker as wallet_address,
        to_hex(t.condition_id) as market_id,
        t.question,                                            
        t.token_outcome_name as outcome,
        t.amount as usd_volume,
        t.price as entry_price,
        CAST(NULL AS VARCHAR) as resolved_outcome,
        CAST(NULL AS DOUBLE) as realized_pnl,
        to_unixtime(t.block_time) as block_timestamp
    FROM polymarket_polygon.market_trades t
    WHERE t.block_time >= CAST('2025-01-01' AS TIMESTAMP)
      AND t.taker IS NOT NULL
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
    
    try:
        batch_counter = 0
        for row in dune.fetch_results_paginated(execution_id):
            blockchain_id = row.get("blockchain_id")
            
            # Simple check: skip if already exists
            if db.query(OnchainTrade).filter_by(blockchain_id=blockchain_id).first():
                continue

            raw_trade = OnchainTrade(
                blockchain_id=blockchain_id,
                wallet_address=row.get("wallet_address"),
                market_id=row.get("market_id"),
                question=row.get("question"),
                outcome=row.get("outcome", "unknown"),
                usd_volume=float(row.get("usd_volume", 0.0)),
                entry_price=float(row.get("entry_price", 0.0)),
                block_timestamp=float(row.get("block_timestamp", time.time()))
            )
            db.add(raw_trade)
            batch_counter += 1

        db.commit()
        logger.info(f"Backfill complete! Committed {batch_counter} new trades.")

    except Exception as e:
        db.rollback()
        logger.error(f"Ingestion failed: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    run_backfill()