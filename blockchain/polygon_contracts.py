"""Polymarket CTF Exchange contract constants (Polygon mainnet).

V1 and V2 coexist: V1 markets still have long-tail activity, V2 is primary
for new markets. See reference/polygon_live_monitor.md for the V1/V2
migration context.

Addresses verified two ways: (1) contract bytecode exists at each address
(eth_getCode, non-empty), and (2) for the V2 addresses, they match
Polymarket's own ctf-exchange-v2 GitHub repo's documented Polygon mainnet
deployment. V1 addresses were NOT independently re-verified against
PolygonScan (no scraping access from this environment -- PolygonScan
blocks it) but are unchanged from reference/polygon_live_monitor.md, which
is presumed reliable for V1 since V2 addresses matched exactly.

IMPORTANT -- the V1 and V2 OrderFilled event ABIs are NOT the same shape,
contradicting reference/polygon_live_monitor.md's claim that "the signature
string is the same but the ABI encoding differs, so the topic0 hashes are
the same." Verified live against Polymarket/ctf-exchange-v2's actual source
(src/exchange/interfaces/ITrading.sol): V2's OrderFilled has a `side`
field typed `Side` (a Solidity enum, ABI-encoded as uint8, not the doc's
claimed uint256) and TWO extra trailing fields (`builder`, `metadata`,
both bytes32) that don't exist in V1's OrderFilled at all. V1 was
independently checked against Polymarket/ctf-exchange's real ITrading.sol
and matches the doc's original 8-field claim exactly -- only V2 was wrong.
This means V1 and V2 have genuinely different topic0 hashes; querying both
versions requires an OR'd topics filter (`topics: [[V1, V2]]`), not one
shared topic0.
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
# -- V1's real 8-field event (see module docstring). Cross-checked by
# test_polygon_contracts.py against a live Web3.keccak() call. The value in
# reference/polygon_live_monitor.md was wrong in its last hex digit
# (...bfec0f5); this is the value Web3.keccak() actually computes (...bfec0f6).
ORDER_FILLED_TOPIC0_V1 = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

# Keccak256 of "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
# -- V2's real 10-field event, confirmed by decoding 657 live logs matched
# against the V2 exchange contracts within an 18-second block window (vs.
# zero matches across a full hour using the doc's claimed 8-field shape).
ORDER_FILLED_TOPIC0_V2 = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"

ORDER_FILLED_ABI_V2 = [{
    "name": "OrderFilled",
    "type": "event",
    "anonymous": False,
    "inputs": [
        {"name": "orderHash", "type": "bytes32", "indexed": True},
        {"name": "maker", "type": "address", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "side", "type": "uint8", "indexed": False},
        {"name": "tokenId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "fee", "type": "uint256", "indexed": False},
        {"name": "builder", "type": "bytes32", "indexed": False},
        {"name": "metadata", "type": "bytes32", "indexed": False},
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
