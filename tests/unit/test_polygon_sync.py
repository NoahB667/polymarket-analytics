import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import OnchainTrade, PolygonSyncState, WalletProfile

from blockchain.polygon_sync import PolygonSyncService
from blockchain.event_decoder import OrderFilledEvent
from blockchain.polygon_contracts import CTF_EXCHANGE_V2


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _service(**overrides):
    kwargs = dict(rpc_url="https://fake-rpc", db_session_factory=_session_factory(), redis_client=MagicMock())
    kwargs.update(overrides)
    return PolygonSyncService(**kwargs)


def _event(tx="0x" + "3" * 64, log_index=0, maker="0xmaker", taker=CTF_EXCHANGE_V2):
    return OrderFilledEvent(
        order_hash="0x" + "1" * 64, maker=maker, taker=taker, token_id="4242",
        usd_amount=5.0, shares=10.0, implied_price=0.5, maker_side="BUY", fee_usd=0.0,
        contract_version="v2", block_number=100, block_timestamp=time.time(),
        tx_hash=tx, log_index=log_index,
    )


def test_get_last_block_reads_redis_first():
    redis_client = MagicMock()
    redis_client.get.return_value = "500"
    service = _service(redis_client=redis_client)
    assert service._get_last_block() == 500


def test_get_last_block_falls_back_to_postgres_then_default():
    redis_client = MagicMock()
    redis_client.get.return_value = None
    session_factory = _session_factory()
    db = session_factory()
    db.add(PolygonSyncState(last_block=777, last_updated=time.time()))
    db.commit()
    db.close()

    service = _service(redis_client=redis_client, db_session_factory=session_factory)
    assert service._get_last_block() == 777


def test_get_last_block_returns_none_when_nothing_stored():
    redis_client = MagicMock()
    redis_client.get.return_value = None
    service = _service(redis_client=redis_client)
    assert service._get_last_block() is None


def test_save_last_block_writes_redis_and_postgres():
    redis_client = MagicMock()
    session_factory = _session_factory()
    service = _service(redis_client=redis_client, db_session_factory=session_factory)

    service._save_last_block(999, events_processed=3)

    redis_client.set.assert_called_with("polygon:last_block", 999)
    db = session_factory()
    row = db.query(PolygonSyncState).first()
    assert row.last_block == 999
    assert row.events_processed == 3


def test_process_batch_inserts_new_onchain_trade_and_increments_wallet():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()

    service._process_batch(db, [_event()])

    trade = db.query(OnchainTrade).first()
    assert trade is not None
    assert trade.wallet_address == "0xmaker"
    profile = db.query(WalletProfile).filter_by(wallet_address="0xmaker").first()
    assert profile.total_trades == 1


def test_process_batch_skips_non_taker_side_events():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()

    service._process_batch(db, [_event(taker="0x" + "9" * 40)])

    assert db.query(OnchainTrade).count() == 0


def test_process_batch_is_idempotent_on_duplicate_blockchain_id():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    event = _event()

    service._process_batch(db, [event])
    service._process_batch(db, [event])  # same tx_hash + log_index

    assert db.query(OnchainTrade).count() == 1
    profile = db.query(WalletProfile).filter_by(wallet_address="0xmaker").first()
    assert profile.total_trades == 1  # not double-counted


def test_process_batch_one_bad_event_does_not_abort_batch():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    good = _event(tx="0x" + "5" * 64)
    bad = _event(tx="0x" + "6" * 64, maker=None)  # None wallet_address -> DB error

    service._process_batch(db, [bad, good])

    assert db.query(OnchainTrade).filter_by(wallet_address="0xmaker").count() == 1


def test_fetch_logs_chunks_large_ranges():
    w3 = MagicMock()
    w3.eth.get_logs.return_value = []
    service = _service()
    service._w3 = w3
    service.max_blocks_per_query = 1000

    logs, last_successful_block = service._fetch_logs(from_block=1, to_block=2500)

    assert w3.eth.get_logs.call_count == 3  # 1-1000, 1001-2000, 2001-2500
    assert last_successful_block == 2500
    assert logs == []


def test_fetch_logs_halves_chunk_on_block_range_error():
    w3 = MagicMock()
    w3.eth.get_logs.side_effect = [
        Exception("block range too large"),  # 1-1000 requested, fails
        [],  # shrunk to 1-500, succeeds
        [],  # next chunk 501-1000, succeeds
    ]
    service = _service()
    service._w3 = w3
    service.max_blocks_per_query = 1000

    logs, last_successful_block = service._fetch_logs(from_block=1, to_block=1000)

    assert service.max_blocks_per_query == 500
    assert logs == []
    assert last_successful_block == 1000  # both chunks actually succeeded, so fully covered


def test_fetch_logs_last_successful_block_reflects_actual_shrunk_coverage():
    """Regression test: _fetch_chunk_with_retry can shrink its range
    mid-retry and succeed on a narrower range than _fetch_logs originally
    requested (chunk_end, computed before the call from the pre-shrink
    max_blocks_per_query). last_successful_block must reflect the ACTUAL
    covered range -- trusting the stale pre-shrink chunk_end silently
    skips the blocks in between forever, contradicting the no-gap
    guarantee. Uses a to_block far past the shrunk range so the outer
    to_block clamp can't coincidentally hide the bug.
    """
    w3 = MagicMock()
    w3.eth.get_logs.side_effect = [
        Exception("block range too large"),  # 1-20 requested (max_blocks_per_query=20), fails
        [],  # shrunk to 1-10 (halved to 10), succeeds
    ]
    service = _service()
    service._w3 = w3
    service.max_blocks_per_query = 20

    with patch("blockchain.polygon_sync.time.sleep"):
        logs, last_successful_block = service._fetch_logs(from_block=1, to_block=100)

    assert last_successful_block == 10  # not 20 -- only blocks 1-10 were actually fetched


def test_fetch_logs_stops_at_first_persistently_failing_chunk():
    """Regression test: discovered live that a real RPC plan can cap
    eth_getLogs far below max_blocks_per_query (10 blocks vs. this
    project's 1000-block default). A chunk that exhausts all retries must
    NOT be silently skipped -- the caller needs to know sync stopped short
    so it doesn't advance last_processed_block past unfetched blocks
    (which would permanently lose that range's trades).
    """
    w3 = MagicMock()
    # First 1000-block chunk (1-1000) succeeds; second chunk (1001-2000)
    # fails all MAX_RETRIES (3) attempts; a third chunk must never be
    # attempted since _fetch_logs should stop at the first failure.
    w3.eth.get_logs.side_effect = [
        [],  # chunk 1: 1-1000, succeeds
        Exception("boom"), Exception("boom"), Exception("boom"),  # chunk 2: exhausts retries
    ]
    service = _service()
    service._w3 = w3
    service.max_blocks_per_query = 1000

    with patch("blockchain.polygon_sync.time.sleep"):
        logs, last_successful_block = service._fetch_logs(from_block=1, to_block=3000)

    assert last_successful_block == 1000  # stopped after the last successful chunk
    assert w3.eth.get_logs.call_count == 4  # 1 success + 3 exhausted retries, chunk 3 never attempted


def test_fetch_chunk_with_retry_returns_failure_after_exhausting_retries():
    w3 = MagicMock()
    w3.eth.get_logs.side_effect = Exception("boom")
    service = _service()
    service._w3 = w3

    with patch("blockchain.polygon_sync.time.sleep"):
        logs, success = service._fetch_chunk_with_retry(1, 10)

    assert success is False
    assert logs == []


def test_decode_logs_caches_block_timestamp_lookups_per_distinct_block():
    """Regression test: discovered live that a single Polygon block can
    carry 40+ OrderFilled logs. An uncached eth_getBlock call per log
    (rather than per distinct block number) turned a real ~100-block batch
    of ~8,400 logs into ~8,400 RPC round-trips (~21 minutes) instead of
    ~100 -- catastrophically slower than the 2-second poll interval.
    """
    service = _service()
    w3 = MagicMock()
    w3.eth.get_block.return_value = {"timestamp": 1234567890}
    service._w3 = w3

    raw_logs = [
        {"blockNumber": 100, "logIndex": 0},
        {"blockNumber": 100, "logIndex": 1},
        {"blockNumber": 100, "logIndex": 2},
        {"blockNumber": 101, "logIndex": 0},
    ]
    with patch("blockchain.polygon_sync.decode_log", return_value=None) as mock_decode:
        service._decode_logs(raw_logs)

    assert w3.eth.get_block.call_count == 2  # once for block 100, once for block 101
    assert mock_decode.call_count == 4  # every log still gets decoded


def test_process_batch_updates_metrics_counters():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()

    service._process_batch(db, [_event(), _event(tx="0x" + "7" * 64, taker="0x" + "9" * 40)])

    assert service.metrics["events_processed_total"] == 1
    assert service.metrics["events_skipped_total"] == 1
    assert service.metrics["wallet_profiles_updated_total"] == 1
