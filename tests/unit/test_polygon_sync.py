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
from models.orm import AutoSubscription, OnchainTrade, PolygonSyncState, WalletProfile

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


def _seed_tracked_market(db, token_id="4242"):
    """Seeds an active AutoSubscription tracking the given token_id, so
    _process_batch's tracked-market filter doesn't exclude test events
    whose actual point is unrelated (dedup, error handling, commit
    behavior, metrics)."""
    db.add(AutoSubscription(
        slug="tracked-market", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", token_ids=[token_id],
    ))
    db.commit()


def _event(tx="0x" + "3" * 64, log_index=0, maker="0xmaker", taker=CTF_EXCHANGE_V2, token_id="4242"):
    return OrderFilledEvent(
        order_hash="0x" + "1" * 64, maker=maker, taker=taker, token_id=token_id,
        usd_amount=5.0, shares=10.0, implied_price=0.5, maker_side="BUY", fee_usd=0.0,
        contract_version="v2", block_number=100, block_timestamp=time.time(),
        tx_hash=tx, log_index=log_index,
    )


class _FakeEth:
    """Minimal eth namespace stand-in for _sync_loop tests. A MagicMock
    can't cleanly distinguish "block_number accessed twice" from "accessed
    three times" for a plain attribute (as opposed to a mocked method with
    call_count) -- this counts accesses explicitly instead.
    """

    def __init__(self, block_number_value):
        self._block_number_value = block_number_value
        self.block_number_access_count = 0

    @property
    def block_number(self):
        self.block_number_access_count += 1
        return self._block_number_value

    def get_logs(self, filter_params):
        return []

    def get_block(self, block_number):
        return {"timestamp": time.time()}


def test_sync_loop_reuses_current_block_for_blocks_behind_metric():
    """Regression test: the blocks_behind metric must reuse current_block
    already fetched this iteration (inside the try/except at the top of
    the loop), not make a second, unguarded eth_blockNumber call. A second
    call sitting outside that try/except would, if it raised (a transient
    RPC blip, rate limit, timeout -- exactly what the rest of this module
    treats as routine and non-fatal), propagate uncaught out of
    _sync_loop, silently killing the daemon thread for good with no
    restart and no retry.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    service.poll_interval_seconds = 0
    service.max_blocks_per_query = 1000

    fake_eth = _FakeEth(block_number_value=100)
    service._w3.eth = fake_eth

    # Run exactly one loop iteration: let the real _stop_event.wait() be
    # called once (with poll_interval_seconds=0, effectively a no-op
    # delay), then set the event so the while condition ends the loop on
    # its next check.
    real_wait = service._stop_event.wait

    def _stop_after_one_iteration(timeout):
        result = real_wait(timeout)
        service._stop_event.set()
        return result

    service._stop_event.wait = _stop_after_one_iteration

    service._sync_loop()

    # Exactly 2 accesses: the pre-loop fetch, plus one per-iteration fetch.
    # The old code's extra call at the blocks_behind line would make this 3.
    assert fake_eth.block_number_access_count == 2
    assert service.metrics["blocks_behind"] == 0
    assert service.metrics["last_synced_block"] == 100


def test_sync_loop_caps_catchup_to_max_catchup_blocks_per_cycle():
    """Regression: when the gap exceeds max_catchup_blocks, _fetch_logs was
    called with to_block=current_block (the raw, unbounded chain tip) every
    single iteration -- max_catchup_blocks was only ever used to decide
    whether to log the "large gap detected" warning, never to actually cap
    per-cycle work. On a real 200k+ block backlog with a 2-second poll
    interval, this meant every cycle attempted hundreds of sequential
    eth_getLogs calls in one unbroken burst, pinning the sync thread's CPU
    continuously since new blocks kept arriving faster than one thread
    could plow through the backlog sequentially -- confirmed live. Each
    cycle must instead do bounded work: to_block capped at
    last_processed + max_catchup_blocks.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    service.poll_interval_seconds = 0
    service.max_catchup_blocks = 100

    fake_eth = _FakeEth(block_number_value=1_000_000)
    service._w3.eth = fake_eth

    with patch.object(service, "_get_last_block", return_value=0), \
         patch.object(service, "_fetch_logs", return_value=([], 100)) as mock_fetch_logs, \
         patch.object(service, "_save_last_block"):

        real_wait = service._stop_event.wait

        def _stop_after_one_iteration(timeout):
            result = real_wait(timeout)
            service._stop_event.set()
            return result

        service._stop_event.wait = _stop_after_one_iteration

        service._sync_loop()

    # Capped to last_processed(0) + max_catchup_blocks(100) = 100, not the
    # raw current_block of 1,000,000.
    mock_fetch_logs.assert_called_once_with(1, 100)


def test_sync_loop_caps_catchup_to_max_chunks_per_cycle_when_chunk_size_is_small():
    """Regression: max_catchup_blocks bounds the block SPAN per cycle, but
    a cycle's actual cost is (span / max_blocks_per_query) sequential
    eth_getLogs calls. Once the block-range auto-shrink correctly detects
    a provider's real per-call limit (confirmed live: as low as 10 blocks
    on a free-tier plan), a single "capped" 10,000-block cycle balloons
    into ~1,000 sequential RPC round-trips -- recreating the unbroken-
    burst, never-idles problem max_catchup_blocks was introduced to fix,
    just measured in requests instead of blocks. max_chunks_per_cycle
    must win when it produces a smaller bound than max_catchup_blocks.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    service.poll_interval_seconds = 0
    service.max_catchup_blocks = 10_000
    service.max_blocks_per_query = 10
    service.max_chunks_per_cycle = 30

    fake_eth = _FakeEth(block_number_value=1_000_000)
    service._w3.eth = fake_eth

    with patch.object(service, "_get_last_block", return_value=0), \
         patch.object(service, "_fetch_logs", return_value=([], 300)) as mock_fetch_logs, \
         patch.object(service, "_save_last_block"):

        real_wait = service._stop_event.wait

        def _stop_after_one_iteration(timeout):
            result = real_wait(timeout)
            service._stop_event.set()
            return result

        service._stop_event.wait = _stop_after_one_iteration

        service._sync_loop()

    # Capped to max_blocks_per_query(10) * max_chunks_per_cycle(30) = 300,
    # not max_catchup_blocks(10_000) and not the raw current_block.
    mock_fetch_logs.assert_called_once_with(1, 300)


def test_sync_loop_survives_exception_in_fetch_and_process_block():
    """Regression: _fetch_logs and everything after it (decode, process,
    save) ran unguarded in _sync_loop -- any exception there propagated
    straight out of the loop, silently killing the daemon thread for good
    with no restart, no retry, and critically no error logged either.
    Confirmed live: the sync thread died silently days before this test was
    written and never logged a single error afterward. One bad cycle must
    be logged and retried next interval, matching this module's existing
    non-fatal-RPC-error philosophy (see the eth_blockNumber try/except).
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    service.poll_interval_seconds = 0

    fake_eth = _FakeEth(block_number_value=100)
    service._w3.eth = fake_eth

    with patch.object(service, "_get_last_block", return_value=0), \
         patch.object(service, "_fetch_logs", side_effect=RuntimeError("boom")):

        real_wait = service._stop_event.wait

        def _stop_after_one_iteration(timeout):
            result = real_wait(timeout)
            service._stop_event.set()
            return result

        service._stop_event.wait = _stop_after_one_iteration

        service._sync_loop()  # must not raise

    assert service.metrics["rpc_errors_total"] == 1


def test_service_configures_http_provider_timeout():
    """Regression: Web3.HTTPProvider had no request timeout configured, so
    a slow/unresponsive RPC endpoint could hang eth_blockNumber or
    eth_getLogs indefinitely -- no exception, no timeout, no progress,
    forever. Confirmed live: the sync thread got stuck on its very first
    RPC call and never logged another line for days, completely and
    silently disabling Polygon on-chain sync. An explicit timeout ensures
    a hung call eventually raises, which the existing try/except blocks
    already treat as a normal, retryable, non-fatal RPC error.
    """
    from web3 import Web3 as RealWeb3

    with patch("blockchain.polygon_sync.Web3.HTTPProvider", wraps=RealWeb3.HTTPProvider) as mock_http_provider:
        _service()

    assert mock_http_provider.call_count == 1
    _, kwargs = mock_http_provider.call_args
    assert kwargs.get("request_kwargs", {}).get("timeout") is not None


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
    _seed_tracked_market(db)

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


def test_process_batch_stores_trade_but_skips_wallet_counters_for_untracked_markets():
    """Wallet scoring (compile_profile) deliberately reads a wallet's
    *entire* cross-market history -- category_concentration, unique_markets,
    etc. -- so narrowing what gets CAPTURED to only tracked markets would
    bias every wallet's profile, not just cut storage waste. The
    OnchainTrade row is always stored regardless of tracked status; only
    the expensive extra work (the wallet counter bump) is scoped to
    markets auto-discovery currently tracks -- there's no value in
    immediately re-scoring a wallet purely because it traded on a market
    nothing here cares about (e.g. sports instead of geopolitics).
    token_id (not condition_id) is the shared identifier space:
    AutoSubscription.token_ids stores the same CLOB token ids the decoded
    OrderFilledEvent carries.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    db.add(AutoSubscription(
        slug="tracked-market", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", token_ids=["some-other-token"],
    ))
    db.commit()

    service._process_batch(db, [_event(token_id="4242")])

    trade = db.query(OnchainTrade).first()
    assert trade is not None  # always stored, regardless of tracked status
    assert trade.market_id == "4242"
    assert db.query(WalletProfile).count() == 0  # counter bump skipped -- not a tracked market
    assert service.metrics["wallet_profiles_updated_total"] == 0
    assert service.metrics["events_processed_total"] == 1  # still counted as processed (stored)


def test_process_batch_bumps_wallet_counters_for_tracked_markets():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    db.add(AutoSubscription(
        slug="tracked-market", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", token_ids=["4242", "other"],
    ))
    db.commit()

    service._process_batch(db, [_event(token_id="4242")])

    assert db.query(OnchainTrade).count() == 1
    profile = db.query(WalletProfile).filter_by(wallet_address="0xmaker").first()
    assert profile is not None
    assert profile.total_trades == 1
    assert service.metrics["wallet_profiles_updated_total"] == 1


def test_process_batch_stores_trade_but_skips_wallet_counters_when_tracked_lookup_fails():
    """A transient failure loading the tracked-token-id set must still
    store the trade (the OnchainTrade row is never conditional on tracked
    status -- see test_process_batch_stores_trade_but_skips_wallet_
    counters_for_untracked_markets), but the extra wallet-counter work is
    conservatively skipped when tracked status can't be determined,
    matching the untracked case: nothing is permanently lost either way,
    since a future confirmed-tracked trade (or a periodic full rescore)
    can still bump this wallet's counters later. Drops just the
    auto_subscription table (rather than mocking db.query broadly) so
    the tracked-lookup query fails with a real error while every other
    query _process_batch makes (the dedup check) still works normally.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    AutoSubscription.__table__.drop(engine)
    session_factory = sessionmaker(bind=engine)
    service = _service(db_session_factory=session_factory)
    db = session_factory()

    service._process_batch(db, [_event(token_id="4242")])

    assert db.query(OnchainTrade).count() == 1
    assert db.query(WalletProfile).count() == 0
    assert service.metrics["wallet_profiles_updated_total"] == 0


def test_process_batch_stores_trade_but_skips_wallet_counters_when_no_markets_tracked_yet():
    """_get_tracked_token_ids returning an empty set (a successful query
    that legitimately found zero active markets, e.g. the cold-start
    window before auto-discovery's first cycle has committed anything)
    must be treated the same as a failed lookup for the counter-bump
    decision: skip it. The trade itself is still always stored.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    # No AutoSubscription rows at all -- a real, successful query that
    # legitimately finds nothing tracked yet, not a DB failure.

    service._process_batch(db, [_event(token_id="4242")])

    assert db.query(OnchainTrade).count() == 1
    assert db.query(WalletProfile).count() == 0
    assert service.metrics["wallet_profiles_updated_total"] == 0


def test_process_batch_is_idempotent_on_duplicate_blockchain_id():
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    _seed_tracked_market(db)
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
    _seed_tracked_market(db)
    good = _event(tx="0x" + "5" * 64)
    bad = _event(tx="0x" + "6" * 64, maker=None)  # None wallet_address -> DB error

    service._process_batch(db, [bad, good])

    assert db.query(OnchainTrade).filter_by(wallet_address="0xmaker").count() == 1


def test_process_batch_rolls_back_trade_when_counter_bump_fails():
    """Regression test: the OnchainTrade insert and the wallet counter bump
    must be ONE transaction. Committing the trade first meant a failure in
    increment_wallet_counters left the trade row committed while its
    counter bump was rolled back -- and since the dedup check skips
    already-present trades, that counter bump was lost permanently on
    every future run, silently corrupting insider_score's inputs.
    """
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    _seed_tracked_market(db)

    with patch(
        "blockchain.polygon_sync.increment_wallet_counters",
        side_effect=Exception("counter bump failed"),
    ):
        processed = service._process_batch(db, [_event()])

    assert processed == 0
    # The trade must NOT survive on its own -- otherwise the dedup check
    # would skip it forever and its counter bump would never happen.
    assert db.query(OnchainTrade).count() == 0
    assert db.query(WalletProfile).count() == 0


def test_process_batch_commits_trade_and_counter_together():
    """The happy path must still durably persist both halves."""
    session_factory = _session_factory()
    service = _service(db_session_factory=session_factory)
    db = session_factory()
    _seed_tracked_market(db)

    service._process_batch(db, [_event()])
    db.close()

    # Re-open a fresh session to prove the write actually committed rather
    # than merely being pending in the original session's transaction.
    verify_db = session_factory()
    assert verify_db.query(OnchainTrade).count() == 1
    profile = verify_db.query(WalletProfile).filter_by(wallet_address="0xmaker").first()
    assert profile is not None
    assert profile.total_trades == 1
    verify_db.close()


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
    # 1-1000 fails, 1-500 (shrunk) succeeds, then a separate 501-1000 chunk
    # succeeds -- 3 calls, not 2. Asserted explicitly so a future edit that
    # shrinks side_effect back to 2 entries fails loudly here instead of
    # masking a real bug behind get_logs' MagicMock StopIteration.
    assert w3.eth.get_logs.call_count == 3


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


def test_fetch_logs_halves_chunk_on_block_range_error_in_response_body():
    """Regression: confirmed live against a real Free-tier Alchemy
    endpoint that a block-range-too-large rejection comes back as a plain
    requests.HTTPError whose str() is just "400 Client Error: Bad Request
    for url: ..." -- the actual detail ("Under the Free tier plan, you can
    make eth_getLogs requests with up to a 10 block range...") lives only
    in the response body (e.response.text), which the old code never
    inspected. The existing "block range" detection checked only str(e),
    so this exact real-world error was silently treated as a generic
    failure every time: max_blocks_per_query (default 1000) never shrank
    below Alchemy's real 10-block limit, so every retry kept requesting a
    range that was always rejected -- the sync thread made zero forward
    progress, indefinitely, while still logging nothing beyond a generic
    per-cycle error (itself masked by the separate unguarded-exception bug
    fixed earlier).
    """
    class _FakeResponse:
        text = (
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":'
            '"Under the Free tier plan, you can make eth_getLogs requests '
            'with up to a 10 block range."}}'
        )

    class _FakeHTTPError(Exception):
        def __init__(self):
            super().__init__("400 Client Error: Bad Request for url: https://example.com/v2/secret-key")
            self.response = _FakeResponse()

    w3 = MagicMock()
    w3.eth.get_logs.side_effect = [
        _FakeHTTPError(),  # 1-1000 requested, real Alchemy-shaped rejection
        [],  # shrunk to 1-500, succeeds
        [],  # next chunk 501-1000, succeeds
    ]
    service = _service()
    service._w3 = w3
    service.max_blocks_per_query = 1000

    logs, last_successful_block = service._fetch_logs(from_block=1, to_block=1000)

    assert service.max_blocks_per_query == 500
    assert logs == []
    assert last_successful_block == 1000


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
    _seed_tracked_market(db)

    service._process_batch(db, [_event(), _event(tx="0x" + "7" * 64, taker="0x" + "9" * 40)])

    assert service.metrics["events_processed_total"] == 1
    assert service.metrics["events_skipped_total"] == 1
    assert service.metrics["wallet_profiles_updated_total"] == 1
