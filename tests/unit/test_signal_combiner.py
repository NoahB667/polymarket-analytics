import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import orjson
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import AutoSubscription, Signal, OnchainTrade, WalletProfile

import analytics.signal_combiner as sc


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_high_insider_wallets(db, market_id: str, count: int = 30) -> None:
    """Seeds `count` distinct wallets, all with insider_score above
    HIGH_INSIDER_SCORE_THRESHOLD (0.6) and one OnchainTrade each in
    `market_id`, so build_signal2_score computes market_insider_risk=1.0
    and a sample_size large enough (relative to its /50 normalizer) to
    push signal2 confidence high enough for a combined TRADE score --
    see the module docstring's SIGNAL2_SAMPLE_SIZE_NORMALIZER note in
    blockchain/wallet_profiler.py.
    """
    now = time.time()
    for i in range(count):
        address = f"0xwallet{i:03d}"
        db.add(OnchainTrade(
            blockchain_id=f"tx-{i:03d}", wallet_address=address, market_id=market_id,
            usd_volume=1000.0, entry_price=0.15, block_timestamp=now,
        ))
        db.add(WalletProfile(wallet_address=address, insider_score=0.8, last_updated=now))
    db.commit()


def _auto_subscription(**overrides):
    defaults = dict(
        slug="test-market",
        question="Q",
        category="politics",
        condition_id="0xabc",
        market_score=0.9,
        tier=1,
        volume_24h=100_000.0,
        subscribed_at=time.time(),
        status="active",
        token_ids=["tok-yes", "tok-no"],
    )
    defaults.update(overrides)
    return AutoSubscription(**defaults)


def test_read_signal1_returns_none_when_uncached():
    redis_client = MagicMock()
    redis_client.get.return_value = None
    assert sc.read_signal1(redis_client, "missing-slug") is None


def test_read_signal1_decodes_cached_payload():
    redis_client = MagicMock()
    redis_client.get.return_value = orjson.dumps({"score": 0.9, "confidence": 0.9, "direction": "BUY", "latest_price": 0.15})
    result = sc.read_signal1(redis_client, "test-market")
    assert result["direction"] == "BUY"
    assert result["latest_price"] == 0.15


def test_build_combined_signal_none_when_no_signal1():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    redis_client = MagicMock()
    redis_client.get.return_value = None  # no signal:1:score cached

    result = sc.build_combined_signal(db, redis_client, _auto_subscription(), has_open_position=False)
    assert result is None


def test_build_combined_signal_none_when_no_condition_id():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    redis_client = MagicMock()
    redis_client.get.return_value = orjson.dumps(
        {"score": 0.9, "confidence": 0.9, "direction": "BUY", "latest_price": 0.15}
    )

    result = sc.build_combined_signal(
        db, redis_client, _auto_subscription(condition_id=None), has_open_position=False
    )
    assert result is None


def test_build_combined_signal_produces_trade_action_when_gates_pass():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    _seed_high_insider_wallets(db, market_id="0xabc", count=30)
    redis_client = MagicMock()

    def fake_get(key):
        if key == "signal:1:score:test-market":
            return orjson.dumps({"score": 0.9, "confidence": 0.9, "direction": "BUY", "latest_price": 0.15})
        return None  # no market:insider_risk cache -- forces a fresh compute from the seeded rows

    redis_client.get.side_effect = fake_get

    result = sc.build_combined_signal(db, redis_client, _auto_subscription(), has_open_position=False)

    # signal1_confidence=0.9, signal2: risk=1.0 (all 30 seeded wallets score
    # 0.8 > HIGH_INSIDER_SCORE_THRESHOLD), sample_size=30 ->
    # confidence = min(30/50, 1.0) * 1.0 = 0.6
    # combined = 0.9*0.6 + 0.6*0.4 = 0.78 > TRADE_THRESHOLD (0.75)
    assert result is not None
    assert result.direction == "BUY"
    assert result.combined_score == 0.78
    assert result.recommended_action == sc.ACTION_TRADE
    assert result.gates_passed is True


def test_build_combined_signal_watch_when_position_already_open():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    redis_client = MagicMock()

    def fake_get(key):
        if key == "signal:1:score:test-market":
            return orjson.dumps({"score": 0.9, "confidence": 0.9, "direction": "BUY", "latest_price": 0.15})
        if key == "market:insider_risk:0xabc":
            return b"0.5"
        return None

    redis_client.get.side_effect = fake_get

    result = sc.build_combined_signal(db, redis_client, _auto_subscription(), has_open_position=True)

    assert result.gates_passed is False
    assert result.recommended_action == sc.ACTION_WATCH


def test_persist_signal_writes_append_only_row():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    combined = sc.CombinedSignal(
        market_id="0xabc", slug="test-market", direction="BUY", combined_score=0.8,
        signal1_confidence=0.9, signal2_confidence=0.5, signal2_market_insider_risk=0.5,
        recommended_action=sc.ACTION_TRADE, gates_passed=True, timestamp=time.time(),
    )

    sc.persist_signal(db, combined)

    rows = db.query(Signal).all()
    assert len(rows) == 1
    assert rows[0].market_id == "0xabc"
    assert rows[0].recommended_action == "TRADE"


def test_run_signal_combiner_cycle_skips_inactive_and_missing_condition_id():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(_auto_subscription(slug="resolved-market", condition_id="0xdef", status="resolved"))
    db.add(_auto_subscription(slug="no-condition-id", condition_id=None))
    db.commit()
    db.close()

    redis_client = MagicMock()
    redis_client.get.return_value = None

    summary = sc.run_signal_combiner_cycle(session_factory, redis_client, open_position_fn=lambda db, mid: False)

    assert summary == {"evaluated": 0, "trade_signals": 0}


def test_run_signal_combiner_cycle_counts_trade_signals():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(_auto_subscription())
    _seed_high_insider_wallets(db, market_id="0xabc", count=30)
    db.close()

    redis_client = MagicMock()

    def fake_get(key):
        if key == "signal:1:score:test-market":
            return orjson.dumps({"score": 0.9, "confidence": 0.9, "direction": "BUY", "latest_price": 0.15})
        return None  # no market:insider_risk cache -- forces a fresh compute from the seeded rows

    redis_client.get.side_effect = fake_get

    summary = sc.run_signal_combiner_cycle(session_factory, redis_client, open_position_fn=lambda db, mid: False)

    assert summary == {"evaluated": 1, "trade_signals": 1}


def test_run_signal_combiner_cycle_does_not_persist_ignore_actions():
    """IGNORE-action markets must not grow the append-only `signal` table
    every cycle (unbounded write volume) -- only TRADE/WATCH are persisted."""
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(_auto_subscription())
    # No seeded insider wallets -> signal2 confidence stays low/zero, and a
    # low signal1 confidence keeps the combined score below the IGNORE
    # threshold (<0.50).
    db.commit()
    db.close()

    redis_client = MagicMock()

    def fake_get(key):
        if key == "signal:1:score:test-market":
            return orjson.dumps({"score": 0.1, "confidence": 0.1, "direction": "BUY", "latest_price": 0.15})
        return None

    redis_client.get.side_effect = fake_get

    summary = sc.run_signal_combiner_cycle(session_factory, redis_client, open_position_fn=lambda db, mid: False)

    assert summary["evaluated"] == 1
    assert summary["trade_signals"] == 0

    db = session_factory()
    try:
        rows = db.query(Signal).all()
        assert rows == []
    finally:
        db.close()
