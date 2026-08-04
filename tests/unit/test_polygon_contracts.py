import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from web3 import Web3
import blockchain.polygon_contracts as pc


def test_topic0_matches_recomputed_keccak():
    sig = "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
    # HexBytes.hex() on web3 7.x returns without a "0x" prefix (plain
    # bytes.hex() semantics) -- normalize both sides before comparing.
    recomputed = Web3.keccak(text=sig).hex().removeprefix("0x")
    assert recomputed.lower() == pc.ORDER_FILLED_TOPIC0.lower().removeprefix("0x")


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
