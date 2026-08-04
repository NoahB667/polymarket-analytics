import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from eth_abi import encode
from web3 import Web3

from blockchain.polygon_contracts import CTF_EXCHANGE_V2
from blockchain.event_decoder import decode_log, is_taker_side, blockchain_id

w3 = Web3()

ORDER_FILLED_SIG = "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"


def _build_v2_log(side=0, maker_amount=5_000_000, taker_amount=10 * 10**18, taker=CTF_EXCHANGE_V2):
    data = encode(
        ["uint256", "uint256", "uint256", "uint256", "uint256"],
        [side, 4242, maker_amount, taker_amount, 0],
    )
    maker = "0x" + "11" * 20
    order_hash = "0x" + "22" * 32
    topics = [
        w3.keccak(text=ORDER_FILLED_SIG),
        bytes.fromhex(order_hash[2:]),
        bytes(12) + bytes.fromhex(maker[2:]),
        bytes(12) + bytes.fromhex(taker[2:].lower()),
    ]
    return {
        "address": Web3.to_checksum_address(CTF_EXCHANGE_V2),
        "topics": topics,
        "data": data,
        "blockNumber": 67543210,
        "transactionHash": bytes.fromhex("33" * 32),
        "transactionIndex": 0,
        "logIndex": 2,
        "blockHash": bytes.fromhex("44" * 32),
    }


def test_decode_v2_buy_event():
    raw_log = _build_v2_log(side=0)
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    assert event is not None
    assert event.contract_version == "v2"
    assert event.maker_side == "BUY"
    assert event.usd_amount == 5.0
    assert event.shares == 10.0
    assert event.implied_price == 0.5
    assert event.token_id == "4242"
    assert event.taker.lower() == CTF_EXCHANGE_V2.lower()


def test_decode_v2_sell_event():
    raw_log = _build_v2_log(side=1)
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    assert event.maker_side == "SELL"


def test_is_taker_side_true_for_exchange_taker():
    raw_log = _build_v2_log()
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    assert is_taker_side(event) is True


def test_is_taker_side_false_for_non_exchange_taker():
    raw_log = _build_v2_log(taker="0x" + "99" * 20)
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    assert is_taker_side(event) is False


def test_decode_returns_none_for_unknown_topic0():
    raw_log = _build_v2_log()
    raw_log["topics"][0] = bytes.fromhex("ab" * 32)
    assert decode_log(raw_log, w3, block_timestamp=1234567890.0) is None


def test_blockchain_id_format():
    raw_log = _build_v2_log()
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    bid = blockchain_id(event)
    assert bid == ("33" * 32).upper() + "-2"


def test_decode_v1_event_infers_side_from_maker_asset_id():
    from blockchain.polygon_contracts import CTF_EXCHANGE_V1

    data = encode(
        ["uint256", "uint256", "uint256", "uint256", "uint256"],
        [0, 4242, 5_000_000, 10 * 10**18, 0],  # makerAssetId=0 -> BUY
    )
    maker = "0x" + "11" * 20
    order_hash = "0x" + "22" * 32
    topics = [
        w3.keccak(text=ORDER_FILLED_SIG),
        bytes.fromhex(order_hash[2:]),
        bytes(12) + bytes.fromhex(maker[2:]),
        bytes(12) + bytes.fromhex(CTF_EXCHANGE_V1[2:].lower()),
    ]
    raw_log = {
        "address": Web3.to_checksum_address(CTF_EXCHANGE_V1),
        "topics": topics,
        "data": data,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("55" * 32),
        "transactionIndex": 0,
        "logIndex": 0,
        "blockHash": bytes.fromhex("66" * 32),
    }
    event = decode_log(raw_log, w3, block_timestamp=1234567890.0)
    assert event.contract_version == "v1"
    assert event.maker_side == "BUY"
    assert event.token_id == "4242"  # takerAssetId
