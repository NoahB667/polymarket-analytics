import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from blockchain.wallet_profiler import (
    profile_wallet,
    profile_all_wallets,
    market_insider_risk,
    build_signal2_score,
)


def _onchain_row(
    wallet_address="0xabc",
    market_id="mkt-1",
    category="fed",
    entry_price=0.10,
    outcome="Yes",
    resolved_outcome="Yes",
    usd_volume=2000.0,
    block_timestamp=None,
    market_end_time=None,
):
    return SimpleNamespace(
        wallet_address=wallet_address,
        market_id=market_id,
        category=category,
        entry_price=entry_price,
        outcome=outcome,
        resolved_outcome=resolved_outcome,
        usd_volume=usd_volume,
        block_timestamp=block_timestamp if block_timestamp is not None else time.time(),
        market_end_time=market_end_time,
    )


def _mock_db(query_results):
    """Builds a MagicMock db session whose .query(...).filter(...).all() returns query_results."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = query_results
    db.query.return_value.filter_by.return_value.first.return_value = None
    return db


def test_profile_wallet_upserts_and_caches():
    rows = [_onchain_row() for _ in range(6)]
    db = _mock_db(rows)
    redis_client = MagicMock()

    profile = profile_wallet(db, "0xabc", redis_client)

    assert profile.wallet_address == "0xabc"
    assert profile.total_trades == 6
    db.merge.assert_called_once()
    redis_client.setex.assert_called_once()
    cache_key = redis_client.setex.call_args[0][0]
    assert cache_key == "wallet:0xabc"


def test_profile_wallet_survives_db_write_failure_and_rolls_back():
    rows = [_onchain_row() for _ in range(3)]
    db = _mock_db(rows)
    db.merge.side_effect = Exception("simulated DB failure")
    redis_client = MagicMock()

    profile = profile_wallet(db, "0xabc", redis_client)

    assert profile.wallet_address == "0xabc"
    assert profile.total_trades == 3
    db.rollback.assert_called_once()
    redis_client.setex.assert_called_once()  # cache write still happens despite DB failure


def test_market_insider_risk_fraction_from_high_score_wallets():
    rows = [
        _onchain_row(wallet_address="0xhigh", usd_volume=3000.0),
        _onchain_row(wallet_address="0xlow", usd_volume=1000.0),
    ]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.return_value = None

    # WalletProfile ORM lookups return high score for 0xhigh, low for 0xlow.
    # Keyed by the filter_by(wallet_address=...) kwarg so this is independent
    # of the (unordered) set iteration order used to dedupe wallets.
    scores_by_wallet = {"0xhigh": 0.9, "0xlow": 0.1}

    def fake_filter_by(**kwargs):
        result = MagicMock()
        result.first.return_value = SimpleNamespace(
            insider_score=scores_by_wallet[kwargs["wallet_address"]]
        )
        return result

    db.query.return_value.filter_by.side_effect = fake_filter_by

    risk = market_insider_risk(db, "mkt-1", redis_client)

    assert risk == 0.75  # 3000 / (3000 + 1000)
    redis_client.setex.assert_called_once()


def test_profile_wallet_survives_redis_cache_failure():
    rows = [_onchain_row() for _ in range(3)]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.setex.side_effect = Exception("simulated redis failure")

    profile = profile_wallet(db, "0xabc", redis_client)

    assert profile.wallet_address == "0xabc"
    db.merge.assert_called_once()  # DB persist still happens despite the redis failure


def test_profile_all_wallets_isolates_per_wallet_failures():
    db = MagicMock()
    db.query.return_value.distinct.return_value.all.return_value = [
        ("0xgood",),
        ("0xbad",),
    ]

    call_count = {"n": 0}

    def filter_side_effect(*args, **kwargs):
        call_count["n"] += 1
        filtered = MagicMock()
        if call_count["n"] == 2:
            # Second wallet's trade fetch blows up inside profile_wallet,
            # before its internal try/except blocks, so the exception
            # propagates up to profile_all_wallets.
            filtered.all.side_effect = Exception("simulated failure for second wallet")
        else:
            filtered.all.return_value = [_onchain_row(wallet_address="0xgood")]
        return filtered

    db.query.return_value.filter.side_effect = filter_side_effect
    db.query.return_value.filter_by.return_value.first.return_value = None
    redis_client = MagicMock()

    profiles = profile_all_wallets(db, redis_client)

    assert len(profiles) == 1
    assert profiles[0].wallet_address == "0xgood"
    assert db.rollback.called


def test_market_insider_risk_returns_zero_for_market_with_no_trades():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    redis_client = MagicMock()
    redis_client.get.return_value = None

    risk = market_insider_risk(db, "empty-market", redis_client)

    assert risk == 0.0


def test_market_insider_risk_uses_redis_cache_hit():
    db = MagicMock()
    redis_client = MagicMock()
    redis_client.get.return_value = b"0.42"

    risk = market_insider_risk(db, "mkt-1", redis_client)

    assert risk == 0.42
    db.query.assert_not_called()  # cache hit means no DB query needed


def test_market_insider_risk_survives_redis_failures():
    rows = [_onchain_row(wallet_address="0xhigh", usd_volume=1000.0)]
    db = _mock_db(rows)
    db.query.return_value.filter_by.return_value.first.return_value = SimpleNamespace(
        insider_score=0.9
    )
    redis_client = MagicMock()
    redis_client.get.side_effect = Exception("redis read boom")
    redis_client.setex.side_effect = Exception("redis write boom")

    risk = market_insider_risk(db, "mkt-1", redis_client)

    assert risk == 1.0  # still computes correctly despite both redis failures


def test_build_signal2_score_confidence_formula():
    rows = [_onchain_row(wallet_address=f"0xw{i}") for i in range(10)]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.return_value = None
    db.query.return_value.filter_by.return_value.first.side_effect = (
        lambda *a, **k: SimpleNamespace(insider_score=0.8)
    )

    signal = build_signal2_score(db, "mkt-1", redis_client)

    assert signal.market_id == "mkt-1"
    assert (
        signal.sample_size == 10
    )  # 10 distinct wallets, each with its own WalletProfile record
    assert 0.0 <= signal.confidence <= 1.0


def test_build_signal2_score_excludes_unprofiled_wallets_from_sample_size():
    rows = [
        _onchain_row(wallet_address="0xprofiled", usd_volume=1000.0),
        _onchain_row(wallet_address="0xunprofiled", usd_volume=1000.0),
    ]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.return_value = None

    def fake_filter_by(*args, **kwargs):
        address = kwargs.get("wallet_address")
        result = MagicMock()
        if address == "0xprofiled":
            result.first.return_value = SimpleNamespace(insider_score=0.9)
        else:
            result.first.return_value = None  # no WalletProfile record yet
        return result

    db.query.return_value.filter_by.side_effect = fake_filter_by

    signal = build_signal2_score(db, "mkt-1", redis_client)

    # Only the profiled wallet counts toward sample_size/avg_insider_score —
    # an unprofiled wallet must not be silently treated as a known score of 0.0.
    assert signal.sample_size == 1
    assert signal.avg_insider_score == 0.9
    # But the unprofiled wallet's volume still counts toward total_volume in
    # the risk fraction (treated as non-suspicious, not excluded entirely).
    assert signal.market_insider_risk == 0.5  # 1000 suspicious / 2000 total


def test_build_signal2_score_uses_redis_cache_hit():
    rows = [_onchain_row(wallet_address=f"0xw{i}") for i in range(3)]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.return_value = b"0.42"
    db.query.return_value.filter_by.return_value.first.side_effect = (
        lambda *a, **k: SimpleNamespace(insider_score=0.8)
    )

    signal = build_signal2_score(db, "mkt-1", redis_client)

    assert signal.market_insider_risk == 0.42
    redis_client.setex.assert_not_called()  # cache hit skips recompute-and-write


def test_build_signal2_score_survives_redis_read_failure():
    rows = [_onchain_row(wallet_address=f"0xw{i}") for i in range(3)]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.side_effect = Exception("redis read boom")
    db.query.return_value.filter_by.return_value.first.side_effect = (
        lambda *a, **k: SimpleNamespace(insider_score=0.8)
    )

    signal = build_signal2_score(db, "mkt-1", redis_client)

    assert signal.market_id == "mkt-1"
    assert signal.sample_size == 3


def test_build_signal2_score_survives_redis_write_failure():
    rows = [_onchain_row(wallet_address=f"0xw{i}") for i in range(3)]
    db = _mock_db(rows)
    redis_client = MagicMock()
    redis_client.get.return_value = None
    redis_client.setex.side_effect = Exception("redis write boom")
    db.query.return_value.filter_by.return_value.first.side_effect = (
        lambda *a, **k: SimpleNamespace(insider_score=0.8)
    )

    signal = build_signal2_score(db, "mkt-1", redis_client)

    assert signal.market_id == "mkt-1"
    assert signal.sample_size == 3
