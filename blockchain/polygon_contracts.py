"""Polymarket CTF Exchange contract constants (Polygon mainnet).

V1 and V2 coexist: V1 markets still have long-tail activity, V2 is primary
for new markets. See reference/polygon_live_monitor.md for the V1/V2
migration context.

IMPORTANT: these addresses were transcribed from reference/polygon_live_monitor.md
and have NOT been independently verified against PolygonScan from this
environment (no network access at authoring time). Verify against
https://polygonscan.com before relying on this in production -- an incorrect
address here means the sync loop silently watches the wrong contract.
"""

# V1 contracts (pre-April 28, 2026)
CTF_EXCHANGE_V1 = "0x4bFb41d5B3570DeFd03C39a9A4D8DE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE_V1 = "0xC5d563A36AE78145C45a50134d48A1215220f80A"

# V2 contracts (April 28, 2026 onwards -- primary going forward)
CTF_EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

# Conditional Tokens Framework (unchanged across V1/V2)
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

TAKER_ADDRESSES = {
    CTF_EXCHANGE_V1.lower(),
    NEG_RISK_CTF_EXCHANGE_V1.lower(),
    CTF_EXCHANGE_V2.lower(),
    NEG_RISK_CTF_EXCHANGE_V2.lower(),
}

V2_EXCHANGE_ADDRESSES = {
    CTF_EXCHANGE_V2.lower(),
    NEG_RISK_CTF_EXCHANGE_V2.lower(),
}

# Keccak256 of "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
# -- cross-checked by test_polygon_contracts.py against a live Web3.keccak() call.
# The value in reference/polygon_live_monitor.md was wrong in its last hex digit
# (...bfec0f5); this is the value Web3.keccak() actually computes (...bfec0f6).
ORDER_FILLED_TOPIC0 = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

ORDER_FILLED_ABI_V2 = [{
    "name": "OrderFilled",
    "type": "event",
    "anonymous": False,
    "inputs": [
        {"name": "orderHash", "type": "bytes32", "indexed": True},
        {"name": "maker", "type": "address", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "side", "type": "uint256", "indexed": False},
        {"name": "tokenId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "fee", "type": "uint256", "indexed": False},
    ],
}]

ORDER_FILLED_ABI_V1 = [{
    "name": "OrderFilled",
    "type": "event",
    "anonymous": False,
    "inputs": [
        {"name": "orderHash", "type": "bytes32", "indexed": True},
        {"name": "maker", "type": "address", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "makerAssetId", "type": "uint256", "indexed": False},
        {"name": "takerAssetId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "fee", "type": "uint256", "indexed": False},
    ],
}]
