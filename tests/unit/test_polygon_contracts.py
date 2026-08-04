import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from web3 import Web3
import blockchain.polygon_contracts as pc


def _keccak_no_prefix(sig: str) -> str:
    # HexBytes.hex() on web3 7.x returns without a "0x" prefix (plain
    # bytes.hex() semantics) -- normalize both sides before comparing.
    return Web3.keccak(text=sig).hex().removeprefix("0x")


def test_topic0_v1_matches_recomputed_keccak():
    sig = "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
    assert _keccak_no_prefix(sig) == pc.ORDER_FILLED_TOPIC0_V1.lower().removeprefix("0x")


def test_topic0_v2_matches_recomputed_keccak():
    # Confirmed live against Polymarket/ctf-exchange-v2's real ITrading.sol
    # (side is a Solidity enum -> uint8, plus trailing builder/metadata
    # bytes32 fields the reference doc never mentioned) -- see
    # polygon_contracts.py's module docstring for how this was verified.
    sig = "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
    assert _keccak_no_prefix(sig) == pc.ORDER_FILLED_TOPIC0_V2.lower().removeprefix("0x")


def test_topic0_v1_and_v2_are_different():
    # Contradicts reference/polygon_live_monitor.md's claim that V1 and V2
    # share one topic0 -- their real event shapes differ, so they don't.
    assert pc.ORDER_FILLED_TOPIC0_V1.lower() != pc.ORDER_FILLED_TOPIC0_V2.lower()


def test_taker_addresses_are_lowercase_and_complete():
    assert pc.TAKER_ADDRESSES == {
        pc.CTF_EXCHANGE_V1.lower(),
        pc.NEG_RISK_CTF_EXCHANGE_V1.lower(),
        pc.CTF_EXCHANGE_V2.lower(),
        pc.NEG_RISK_CTF_EXCHANGE_V2.lower(),
    }


def test_v2_exchange_addresses_subset_of_taker_addresses():
    assert pc.V2_EXCHANGE_ADDRESSES.issubset(pc.TAKER_ADDRESSES)
    assert pc.CTF_EXCHANGE_V2.lower() in pc.V2_EXCHANGE_ADDRESSES
    assert pc.NEG_RISK_CTF_EXCHANGE_V2.lower() in pc.V2_EXCHANGE_ADDRESSES


def test_order_filled_abi_v2_has_ten_fields():
    # Confirmed live: V2's real OrderFilled has 10 fields (3 indexed + 7
    # data), not the 8 the reference doc claimed.
    assert len(pc.ORDER_FILLED_ABI_V2[0]["inputs"]) == 10
    field_names = [f["name"] for f in pc.ORDER_FILLED_ABI_V2[0]["inputs"]]
    assert field_names == [
        "orderHash", "maker", "taker", "side", "tokenId",
        "makerAmountFilled", "takerAmountFilled", "fee", "builder", "metadata",
    ]
    side_field = pc.ORDER_FILLED_ABI_V2[0]["inputs"][3]
    assert side_field["name"] == "side"
    assert side_field["type"] == "uint8"  # Solidity enum, not uint256


def test_order_filled_abi_v1_has_eight_fields():
    assert len(pc.ORDER_FILLED_ABI_V1[0]["inputs"]) == 8
