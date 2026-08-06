"""Manual, read-only smoke test for the Polygon live monitor (Step 9) against
a real RPC endpoint. NOT part of the pytest suite -- run directly:

    python3 scripts/smoke_test_polygon_rpc.py [--chunk-size 9] [--max-chunks 50]

Scans backward from the chain head in small eth_getLogs chunks (default 9
blocks each -- this project's free-tier Alchemy key caps eth_getLogs at a
10-block range, contradicting reference/polygon_live_monitor.md's claimed
2,000-block limit), stopping as soon as it finds any OrderFilled activity.
Never writes to Postgres or Redis, never prints the RPC URL (which may
embed an API key) or lets a raw exception traceback escape (requests'
HTTPError embeds the full request URL in its default __str__).
"""

import argparse
import logging
import os
import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[1]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from dotenv import load_dotenv
from web3 import Web3

from blockchain.log_sanitizer import redact_urls

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("smoke_test_polygon_rpc")


def _get_logs_safely(w3: Web3, filter_params: dict):
    """Wraps eth_getLogs so a failure never lets a raw exception (which may
    embed the RPC URL/API key) propagate to the default traceback printer.

    Returns:
        (logs, error_message) -- exactly one of the two is non-None/non-empty.
    """
    try:
        return w3.eth.get_logs(filter_params), None
    except Exception as e:
        response = getattr(e, "response", None)
        if response is not None:
            return None, f"HTTP {response.status_code} -- {redact_urls(response.text[:300])}"
        return None, redact_urls(e)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk-size", type=int, default=9, help="Blocks per eth_getLogs call")
    parser.add_argument("--max-chunks", type=int, default=50, help="Max eth_getLogs calls before giving up")
    args = parser.parse_args()

    rpc_url = os.getenv("POLYGON_RPC_URL")
    if not rpc_url:
        logger.error("POLYGON_RPC_URL is not set in the environment/.env")
        return 1
    logger.info(f"Using RPC endpoint: {rpc_url[:20]}... (truncated, not logging the key)")

    from blockchain.polygon_contracts import TAKER_ADDRESSES, ORDER_FILLED_TOPIC0_V1, ORDER_FILLED_TOPIC0_V2
    from blockchain.event_decoder import decode_log, is_taker_side
    from web3.middleware import ExtraDataToPOAMiddleware

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    # Polygon is PoA -- without this, eth.get_block() (used below to resolve
    # block timestamps) raises ExtraDataLengthError on every call.
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        logger.error("web3 could not connect to the RPC endpoint")
        return 1

    current_block = w3.eth.block_number
    logger.info(f"Chain head: block {current_block}")
    logger.info(
        f"Scanning backward in {args.chunk_size}-block chunks, up to {args.max_chunks} "
        f"calls ({args.chunk_size * args.max_chunks} blocks / "
        f"~{args.chunk_size * args.max_chunks * 2 / 60:.1f} min of history), "
        f"stopping at the first chunk with any activity"
    )

    address_filter = [Web3.to_checksum_address(a) for a in TAKER_ADDRESSES]
    raw_logs = []
    scanned_from = current_block

    for i in range(args.max_chunks):
        to_block = current_block - i * args.chunk_size
        from_block = to_block - args.chunk_size + 1
        scanned_from = from_block

        logs, error = _get_logs_safely(w3, {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": address_filter,
            "topics": [[ORDER_FILLED_TOPIC0_V1, ORDER_FILLED_TOPIC0_V2]],
        })
        if error is not None:
            logger.error(f"eth_getLogs failed on chunk {from_block}-{to_block}: {error}")
            return 1

        if logs:
            raw_logs = logs
            logger.info(f"Found {len(logs)} raw log(s) in blocks {from_block}-{to_block} (call {i + 1}/{args.max_chunks})")
            break
    else:
        logger.warning(
            f"No OrderFilled logs found across {args.max_chunks} chunks "
            f"(blocks {scanned_from}-{current_block}). This does NOT necessarily mean the "
            "contract addresses are wrong -- try a larger --max-chunks, or verify "
            "activity directly on PolygonScan for one of the addresses in "
            "blockchain/polygon_contracts.py."
        )
        return 0

    # Resolve block timestamps for the (small) set of distinct blocks touched.
    block_timestamps = {}
    decoded_count = 0
    taker_side_count = 0
    v1_count = 0
    v2_count = 0
    sample = None

    for raw_log in raw_logs:
        block_number = raw_log["blockNumber"]
        if block_number not in block_timestamps:
            block_timestamps[block_number] = float(w3.eth.get_block(block_number)["timestamp"])

        event = decode_log(raw_log, w3, block_timestamp=block_timestamps[block_number])
        if event is None:
            logger.warning(f"Failed to decode log at tx={raw_log.get('transactionHash')}")
            continue
        decoded_count += 1
        if event.contract_version == "v1":
            v1_count += 1
        else:
            v2_count += 1
        if is_taker_side(event):
            taker_side_count += 1
            if sample is None:
                sample = event

    logger.info(f"Decoded successfully: {decoded_count}/{len(raw_logs)}")
    logger.info(f"  v1: {v1_count}, v2: {v2_count}")
    logger.info(f"Taker-side (canonical, de-duplicated) events: {taker_side_count}")

    if sample is not None:
        logger.info("Sample decoded taker-side event:")
        logger.info(f"  maker={sample.maker}")
        logger.info(f"  token_id={sample.token_id}")
        logger.info(f"  usd_amount={sample.usd_amount:.2f}  shares={sample.shares:.4f}  implied_price={sample.implied_price:.4f}")
        logger.info(f"  maker_side={sample.maker_side}  contract_version={sample.contract_version}")
        logger.info(f"  block={sample.block_number}  tx={sample.tx_hash}")

    if decoded_count == 0:
        logger.error("Found raw logs but decoded none -- ABI or topic0 likely mismatched. Investigate before trusting the addresses.")
        return 1

    logger.info("Smoke test passed: contract addresses are live and OrderFilled logs decode correctly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Last-resort guard: never let a raw traceback escape, since
        # requests/web3 exceptions often embed the full RPC URL (which may
        # contain an API key) in their default string representation.
        logger.error(f"Smoke test failed with an unexpected {type(e).__name__}: {redact_urls(e)}")
        sys.exit(1)
