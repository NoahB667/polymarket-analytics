"""Decodes raw eth_getLogs OrderFilled entries into OrderFilledEvent for
both V1 (makerAssetId/takerAssetId, USDC.e) and V2 (explicit side, pUSD)
CTF Exchange contracts. See reference/polygon_live_monitor.md for the wire
format and V1/V2 differences.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from web3 import Web3

from blockchain.polygon_contracts import (
    ORDER_FILLED_ABI_V1,
    ORDER_FILLED_ABI_V2,
    ORDER_FILLED_TOPIC0,
    TAKER_ADDRESSES,
    V2_EXCHANGE_ADDRESSES,
)

logger = logging.getLogger("polymarket.blockchain.event_decoder")

USD_DECIMALS = 1_000_000
SHARE_DECIMALS = 1_000_000_000_000_000_000


@dataclass
class OrderFilledEvent:
    """A decoded, de-duplication-ready OrderFilled log."""

    order_hash: str
    maker: str
    taker: str
    token_id: str
    usd_amount: float
    shares: float
    implied_price: float
    maker_side: str  # "BUY" or "SELL"
    fee_usd: float
    contract_version: str  # "v1" or "v2"
    block_number: int
    block_timestamp: float
    tx_hash: str
    log_index: int


def _to_hex(value: Any) -> str:
    """Normalizes a bytes-like or str value to a "0x"-prefixed lowercase hex string."""
    text = value.hex() if isinstance(value, (bytes, bytearray)) else str(value)
    if not text.startswith("0x"):
        text = "0x" + text
    return text


def decode_log(raw_log: Dict[str, Any], w3: Web3, block_timestamp: float) -> Optional[OrderFilledEvent]:
    """Decodes a raw eth_getLogs entry into an OrderFilledEvent.

    Args:
        raw_log: Raw log dict as returned by web3.py's eth_getLogs. Must
            include 'transactionIndex' -- web3 7.x's process_log requires
            it even though it's unused by this decoder.
        w3: A Web3 instance, used only for ABI decoding (no network calls).
        block_timestamp: Unix timestamp of raw_log['blockNumber'], resolved
            by the caller (batched to avoid one eth_getBlock call per log).

    Returns:
        The decoded event, or None if topic0 doesn't match OrderFilled (an
        unrelated log slipped through the address filter) or decoding fails.
    """
    topic0_hex = _to_hex(raw_log["topics"][0])
    if topic0_hex.lower() != ORDER_FILLED_TOPIC0.lower():
        return None

    address = raw_log["address"].lower()
    version = "v2" if address in V2_EXCHANGE_ADDRESSES else "v1"
    abi = ORDER_FILLED_ABI_V2 if version == "v2" else ORDER_FILLED_ABI_V1

    try:
        contract = w3.eth.contract(abi=abi)
        decoded = contract.events.OrderFilled().process_log(raw_log)
        args = decoded["args"]
    except Exception as e:
        logger.error(f"Failed to decode OrderFilled log at tx={raw_log.get('transactionHash')}: {e}")
        return None

    maker_amount = args["makerAmountFilled"]
    taker_amount = args["takerAmountFilled"]
    usd_amount = maker_amount / USD_DECIMALS
    shares = taker_amount / SHARE_DECIMALS
    implied_price = usd_amount / shares if shares > 0 else 0.0

    if version == "v2":
        maker_side = "BUY" if args["side"] == 0 else "SELL"
        token_id = str(args["tokenId"])
    else:
        maker_side = "BUY" if args["makerAssetId"] == 0 else "SELL"
        token_id = str(args["takerAssetId"])

    return OrderFilledEvent(
        order_hash=_to_hex(args["orderHash"]),
        maker=args["maker"],
        taker=args["taker"],
        token_id=token_id,
        usd_amount=usd_amount,
        shares=shares,
        implied_price=implied_price,
        maker_side=maker_side,
        fee_usd=args["fee"] / USD_DECIMALS,
        contract_version=version,
        block_number=raw_log["blockNumber"],
        block_timestamp=block_timestamp,
        tx_hash=_to_hex(raw_log["transactionHash"]),
        log_index=raw_log["logIndex"],
    )


def is_taker_side(event: OrderFilledEvent) -> bool:
    """Returns True for the canonical (non-duplicate) leg of a CLOB match.

    Every match emits two OrderFilled events (maker leg + taker leg);
    processing both double-counts wallet activity. Only the leg where
    `taker` is a Polymarket exchange contract is canonical.
    """
    return event.taker.lower() in TAKER_ADDRESSES


def blockchain_id(event: OrderFilledEvent) -> str:
    """Builds the same dedup key format Dune ingestion uses, so a trade
    caught live and later re-seen via Dune backfill collides on one row
    instead of duplicating (see core/wallet_intelligence_scheduler.py's
    `upper(to_hex(t.tx_hash)) || '-' || CAST(t.evt_index AS VARCHAR)`).
    """
    tx_hex = event.tx_hash[2:] if event.tx_hash.startswith("0x") else event.tx_hash
    return f"{tx_hex.upper()}-{event.log_index}"
