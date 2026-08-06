"""Background HTTP-polling service that watches Polygon for Polymarket
OrderFilled settlements and keeps OnchainTrade/WalletProfile fresh between
daily Dune wallet-intelligence cycles. See reference/polygon_live_monitor.md.

Runs on its own daemon thread; never touches the WebSocket hot path.

metrics is a plain dict rather than a prometheus_client object -- Step 13
(core/metrics_exporter.py) doesn't exist yet in this codebase. Once it
does, it can wrap these counters in Gauges/Counters without touching this
module's control flow.
"""

import logging
import os
import threading
import time
from typing import Any, List, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from blockchain.event_decoder import blockchain_id, decode_log, is_taker_side, OrderFilledEvent
from blockchain.log_sanitizer import redact_urls
from blockchain.polygon_contracts import TAKER_ADDRESSES, ORDER_FILLED_TOPIC0_V1, ORDER_FILLED_TOPIC0_V2
from blockchain.wallet_profiler import increment_wallet_counters
from models.orm import OnchainTrade, PolygonSyncState

logger = logging.getLogger("polymarket.blockchain.polygon_sync")

DEFAULT_MAX_BLOCKS_PER_QUERY = int(os.getenv("POLYGON_MAX_BLOCKS_PER_QUERY", "1000"))
DEFAULT_POLL_INTERVAL_SECONDS = float(os.getenv("POLYGON_POLL_INTERVAL_SECONDS", "2"))
DEFAULT_MAX_CATCHUP_BLOCKS = int(os.getenv("POLYGON_MAX_CATCHUP_BLOCKS", "10000"))
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
LAST_BLOCK_REDIS_KEY = "polygon:last_block"


class PolygonSyncService:
    """Polls Polygon for OrderFilled logs and updates wallet profiles.

    Args:
        rpc_url: HTTPS Polygon RPC endpoint. WSS is not supported by this
            implementation -- reference/polygon_live_monitor.md recommends
            HTTP polling (Approach A) for this project's latency needs.
        db_session_factory: Callable returning a new SQLAlchemy session
            (e.g. db.SessionLocal). A fresh session is opened per batch,
            never shared with the FastAPI request threads.
        redis_client: Client matching redis_config.r's interface.
    """

    def __init__(self, rpc_url: str, db_session_factory: Any, redis_client: Any) -> None:
        self.rpc_url = rpc_url
        self.db_session_factory = db_session_factory
        self.redis_client = redis_client
        self.max_blocks_per_query = DEFAULT_MAX_BLOCKS_PER_QUERY
        self.poll_interval_seconds = DEFAULT_POLL_INTERVAL_SECONDS
        self.max_catchup_blocks = DEFAULT_MAX_CATCHUP_BLOCKS
        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        # Polygon is a PoA-style chain -- its block headers' extraData field
        # exceeds the 32 bytes web3.py's default block formatter expects,
        # raising ExtraDataLengthError on every eth_getBlock call (used
        # below to resolve block timestamps) without this. Confirmed live:
        # _decode_logs crashed on the very first real batch without it.
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.metrics = {
            "last_synced_block": 0,
            "blocks_behind": 0,
            "events_processed_total": 0,
            "events_skipped_total": 0,
            "rpc_errors_total": 0,
            "wallet_profiles_updated_total": 0,
        }

    def start(self) -> None:
        """Starts the polling loop on a daemon thread. Returns immediately."""
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the loop to stop and waits for the current batch to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval_seconds * 5)

    def _sync_loop(self) -> None:
        try:
            current_block = self._w3.eth.block_number
        except Exception as e:
            # Never interpolate self.rpc_url or a raw exception here -- RPC
            # URLs commonly embed an API key (e.g. Alchemy's /v2/<key>
            # path), and some HTTP client exceptions embed the full request
            # URL in their default __str__. redact_urls() strips those while
            # keeping the rest of the message's diagnostic value.
            logger.error(f"Polygon sync: could not reach RPC endpoint on startup: {redact_urls(e)}")
            return

        last_processed = self._get_last_block()
        if last_processed is None:
            last_processed = current_block - 100
        if last_processed > current_block:
            logger.warning(f"Polygon sync: last_processed {last_processed} > current {current_block}, resetting")
            last_processed = current_block - 100

        gap = current_block - last_processed
        if gap > self.max_catchup_blocks:
            logger.warning(f"Polygon sync: large gap detected ({gap} blocks), catching up...")

        while not self._stop_event.is_set():
            try:
                current_block = self._w3.eth.block_number
            except Exception as e:
                self.metrics["rpc_errors_total"] += 1
                logger.error(f"Polygon sync: eth_blockNumber failed: {redact_urls(e)}")
                self._stop_event.wait(self.poll_interval_seconds)
                continue

            if current_block > last_processed:
                logs, last_successful_block = self._fetch_logs(last_processed + 1, current_block)
                db = self.db_session_factory()
                try:
                    events_processed = self._process_batch(db, self._decode_logs(logs))
                finally:
                    db.close()
                # Only advance past blocks eth_getLogs actually returned --
                # if a chunk exhausted its retries (e.g. the RPC's real
                # eth_getLogs range cap is far below max_blocks_per_query,
                # observed live to be as low as 10 blocks on some plans vs.
                # this project's 1000-block default), last_successful_block
                # stops short of current_block so those blocks are retried
                # next cycle instead of being silently skipped forever.
                last_processed = last_successful_block
                self._save_last_block(last_processed, events_processed)
                self.metrics["last_synced_block"] = last_processed
                # Reuses current_block fetched at the top of this iteration
                # (line 112) rather than a second eth_blockNumber call --
                # that second call was outside the try/except above, so an
                # RPC blip here would raise uncaught out of _sync_loop and
                # silently kill the daemon thread for good (no restart, no
                # retry), contradicting every other RPC call in this module
                # being treated as non-fatal.
                self.metrics["blocks_behind"] = max(0, current_block - last_processed)

            self._stop_event.wait(self.poll_interval_seconds)

    def _decode_logs(self, logs: List[Any]) -> List[OrderFilledEvent]:
        """Decodes raw logs, resolving each distinct block's timestamp only
        once. Confirmed live: a single block can carry 40+ OrderFilled logs,
        so an uncached eth_getBlock call per log (rather than per distinct
        block number) turned a ~100-block batch of ~8,400 logs into ~8,400
        RPC round-trips (~21 minutes) instead of ~100 -- catastrophically
        slower than the 2-second poll interval, guaranteeing the service
        would fall permanently behind in real usage.
        """
        block_timestamps: dict = {}
        events = []
        for raw_log in logs:
            try:
                block_number = raw_log["blockNumber"]
                if block_number not in block_timestamps:
                    block_timestamps[block_number] = float(self._w3.eth.get_block(block_number)["timestamp"])
                event = decode_log(raw_log, self._w3, block_timestamp=block_timestamps[block_number])
            except Exception as e:
                # Wraps eth_getBlock (an RPC call) as well as pure decoding,
                # so this is equally exposed to URL-bearing exceptions.
                logger.error(f"Polygon sync: failed to decode log: {redact_urls(e)}")
                continue
            if event is not None:
                events.append(event)
        return events

    def _fetch_logs(self, from_block: int, to_block: int) -> "tuple[List[Any], int]":
        """Chunked eth_getLogs across [from_block, to_block], filtered to
        OrderFilled on the known exchange contracts, with retry/backoff.

        Stops at the first chunk that exhausts its retries rather than
        skipping past it -- a gap here would otherwise let the caller
        advance last_processed_block beyond blocks that were never
        actually fetched, permanently losing that range's trades.

        Returns:
            (logs, last_successful_block) -- last_successful_block is the
            highest block number successfully covered without a gap; it
            equals from_block - 1 if even the first chunk failed.
        """
        all_logs: List[Any] = []
        chunk_start = from_block
        last_successful_block = from_block - 1
        while chunk_start <= to_block:
            chunk_end = min(chunk_start + self.max_blocks_per_query - 1, to_block)
            logs, success = self._fetch_chunk_with_retry(chunk_start, chunk_end)
            if not success:
                break
            all_logs.extend(logs)
            # Not chunk_end -- _fetch_chunk_with_retry can shrink
            # self.max_blocks_per_query (and its own toBlock) mid-retry and
            # succeed on a narrower range than chunk_end originally
            # requested. self.max_blocks_per_query at this point always
            # reflects whatever range the successful attempt actually
            # covered, so recompute from chunk_start with the current
            # (possibly now-smaller) value rather than trusting the
            # pre-call chunk_end -- otherwise blocks between the actual
            # covered range and chunk_end are silently skipped forever.
            covered_end = min(chunk_start + self.max_blocks_per_query - 1, to_block)
            last_successful_block = covered_end
            chunk_start = covered_end + 1
        return all_logs, last_successful_block

    def _fetch_chunk_with_retry(self, from_block: int, to_block: int) -> "tuple[List[Any], bool]":
        filter_params = {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": [Web3.to_checksum_address(a) for a in TAKER_ADDRESSES],
            # V1 and V2 have different OrderFilled topic0 values (see
            # polygon_contracts.py's module docstring) -- a nested list at a
            # topics position is an OR filter, matching either.
            "topics": [[ORDER_FILLED_TOPIC0_V1, ORDER_FILLED_TOPIC0_V2]],
        }
        for attempt in range(MAX_RETRIES):
            try:
                return self._w3.eth.get_logs(filter_params), True
            except Exception as e:
                self.metrics["rpc_errors_total"] += 1
                message = str(e).lower()
                if "block range" in message:
                    self.max_blocks_per_query = max(1, self.max_blocks_per_query // 2)
                    logger.warning(f"Polygon sync: block range too large, reduced chunk to {self.max_blocks_per_query}")
                    filter_params["toBlock"] = min(to_block, from_block + self.max_blocks_per_query - 1)
                elif "rate limit" in message:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                else:
                    logger.error(f"Polygon sync: eth_getLogs failed ({from_block}-{to_block}): {redact_urls(e)}")
                    time.sleep(RETRY_DELAY_SECONDS)
        logger.error(f"Polygon sync: eth_getLogs failed after {MAX_RETRIES} retries, skipping {from_block}-{to_block}")
        return [], False

    def _process_batch(self, db: Any, events: List[OrderFilledEvent]) -> int:
        """Filters to taker-side events, dedupes, writes OnchainTrade rows,
        and bumps wallet counters. Best-effort: one bad event is logged and
        skipped, never aborts the batch.

        Returns:
            Count of events successfully processed.
        """
        processed = 0
        for event in events:
            if not is_taker_side(event):
                self.metrics["events_skipped_total"] += 1
                continue
            bid = blockchain_id(event)
            try:
                if db.query(OnchainTrade).filter_by(blockchain_id=bid).first():
                    continue
                db.add(OnchainTrade(
                    blockchain_id=bid,
                    wallet_address=event.maker,
                    market_id=event.token_id,
                    usd_volume=event.usd_amount,
                    entry_price=event.implied_price,
                    block_timestamp=event.block_timestamp,
                ))
                # Deliberately NO commit here -- the OnchainTrade insert and
                # the wallet counter bump must land in ONE transaction
                # (increment_wallet_counters issues the commit covering
                # both). Committing the trade first meant that if the
                # counter bump then raised, the rollback could only undo the
                # counter work: the trade row survived, so the dedup check
                # above would `continue` past it on every future run and the
                # counter bump would be lost permanently -- silently
                # understating total_trades/long_shot_attempts and leaving
                # score_stale unset, corrupting insider_score's inputs with
                # no error trace after the fact.
                increment_wallet_counters(db, event)
                processed += 1
                self.metrics["events_processed_total"] += 1
                self.metrics["wallet_profiles_updated_total"] += 1
            except Exception as e:
                db.rollback()
                logger.error(f"Polygon sync: failed to process event tx={event.tx_hash}: {redact_urls(e)}")
        return processed

    def _get_last_block(self) -> Optional[int]:
        try:
            cached = self.redis_client.get(LAST_BLOCK_REDIS_KEY)
            if cached is not None:
                return int(cached)
        except Exception as e:
            logger.warning(f"Polygon sync: Redis read failed for last_block: {redact_urls(e)}")

        db = self.db_session_factory()
        try:
            row = db.query(PolygonSyncState).order_by(PolygonSyncState.id.desc()).first()
            return row.last_block if row else None
        except Exception as e:
            logger.warning(f"Polygon sync: Postgres read failed for last_block: {redact_urls(e)}")
            return None
        finally:
            db.close()

    def _save_last_block(self, block_num: int, events_processed: int) -> None:
        try:
            self.redis_client.set(LAST_BLOCK_REDIS_KEY, block_num)
        except Exception as e:
            logger.warning(f"Polygon sync: Redis write failed for last_block: {redact_urls(e)}")

        db = self.db_session_factory()
        try:
            db.add(PolygonSyncState(
                last_block=block_num,
                last_updated=time.time(),
                events_processed=events_processed,
            ))
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Polygon sync: Postgres write failed for last_block: {redact_urls(e)}")
        finally:
            db.close()
