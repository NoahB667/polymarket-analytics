import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import AutoSubscription, OnchainTrade

import core.wallet_intelligence_scheduler as wis


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_get_active_condition_ids_excludes_resolved_and_null():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="a", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", condition_id="0xabc",
    ))
    db.add(AutoSubscription(
        slug="b", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="resolved", condition_id="0xdef",
    ))
    db.add(AutoSubscription(
        slug="c", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", condition_id=None,
    ))
    db.commit()
    db.close()

    ids = wis.get_active_condition_ids(session_factory)
    assert ids == ["0xabc"]


def test_sanitize_condition_ids_drops_malformed_entries():
    result = wis._sanitize_condition_ids(["0xabc123", "not-hex", "", None, "0xDEF456"])
    assert result == ["0xabc123", "0xDEF456"]


def test_build_wallet_intelligence_query_returns_none_for_empty_list():
    assert wis.build_wallet_intelligence_query([]) is None
    assert wis.build_wallet_intelligence_query(["not-valid"]) is None


def test_build_wallet_intelligence_query_includes_all_condition_ids():
    sql = wis.build_wallet_intelligence_query(["0xabc123", "0xdef456"], lookback_days=2, min_usd=100, row_limit=5000)
    assert "'0xabc123'" in sql
    assert "'0xdef456'" in sql
    assert "t.amount >= 100" in sql
    assert "LIMIT 5000" in sql
    assert "t.is_taker_side = TRUE" in sql
    assert "t.action = 'clob'" in sql


def test_run_wallet_intelligence_cycle_skips_when_no_condition_ids():
    session_factory = _sqlite_session_factory()

    summary = wis.run_wallet_intelligence_cycle(session_factory=session_factory)

    assert summary == {"condition_ids": 0, "ingested": 0, "profiled": 0}


def test_run_wallet_intelligence_cycle_ingests_and_dedupes_by_blockchain_id():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="a", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", condition_id="0xabc123",
    ))
    # Pre-existing row -- should be skipped as a duplicate, not double-counted.
    db.add(OnchainTrade(
        blockchain_id="TX1-0", wallet_address="0xwallet1", market_id="0xabc123",
        usd_volume=500.0, entry_price=0.5, block_timestamp=time.time(),
    ))
    db.commit()
    db.close()

    fake_dune = MagicMock()
    fake_dune.execute_raw_sql.return_value = "exec-123"
    fake_dune.poll_execution_status.return_value = True
    fake_dune.fetch_results_paginated.return_value = iter([
        {  # duplicate of the pre-existing row -- must not be re-inserted
            "blockchain_id": "TX1-0", "wallet_address": "0xwallet1", "market_id": "0xabc123",
            "usd_volume": 500.0, "entry_price": 0.5, "block_timestamp": time.time(),
            "event_market_name": "Fed rate cut", "outcome": "Yes",
        },
        {  # genuinely new row
            "blockchain_id": "TX2-0", "wallet_address": "0xwallet2", "market_id": "0xabc123",
            "usd_volume": 750.0, "entry_price": 0.6, "block_timestamp": time.time(),
            "event_market_name": "Fed rate cut", "outcome": "No",
        },
    ])

    with patch.object(wis, "MarketResolutionClient") as mock_resolution_cls:
        mock_resolution_cls.return_value.resolve_market.return_value = wis.MarketResolution(
            resolved_outcome=None, market_end_time=None
        )
        summary = wis.run_wallet_intelligence_cycle(
            session_factory=session_factory, dune_client=fake_dune,
        )

    assert summary["condition_ids"] == 1
    assert summary["ingested"] == 1  # only TX2-0 is new

    db = session_factory()
    assert db.query(OnchainTrade).count() == 2
    db.close()


def test_run_wallet_intelligence_cycle_profiles_wallets_when_redis_client_given():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="a", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", condition_id="0xabc123",
    ))
    db.commit()
    db.close()

    fake_dune = MagicMock()
    fake_dune.execute_raw_sql.return_value = "exec-123"
    fake_dune.poll_execution_status.return_value = True
    fake_dune.fetch_results_paginated.return_value = iter([
        {
            "blockchain_id": "TX1-0", "wallet_address": "0xwallet1", "market_id": "0xabc123",
            "usd_volume": 500.0, "entry_price": 0.5, "block_timestamp": time.time(),
            "event_market_name": "Fed rate cut", "outcome": "Yes",
        },
    ])
    fake_redis = MagicMock()

    with patch.object(wis, "MarketResolutionClient") as mock_resolution_cls:
        mock_resolution_cls.return_value.resolve_market.return_value = wis.MarketResolution(
            resolved_outcome=None, market_end_time=None
        )
        summary = wis.run_wallet_intelligence_cycle(
            redis_client=fake_redis, session_factory=session_factory, dune_client=fake_dune,
        )

    assert summary["ingested"] == 1
    assert summary["profiled"] == 1


def test_run_wallet_intelligence_cycle_survives_dune_failure():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="a", question="Q", category="c", market_score=0.9, tier=1,
        subscribed_at=time.time(), status="active", condition_id="0xabc123",
    ))
    db.commit()
    db.close()

    fake_dune = MagicMock()
    fake_dune.execute_raw_sql.side_effect = Exception("Dune API down")

    summary = wis.run_wallet_intelligence_cycle(session_factory=session_factory, dune_client=fake_dune)

    assert summary["ingested"] == 0


def test_run_wallet_intelligence_loop_disabled_by_default_does_nothing():
    with patch.object(wis, "run_wallet_intelligence_cycle") as mock_cycle:
        wis.run_wallet_intelligence_loop()

    mock_cycle.assert_not_called()


def test_run_wallet_intelligence_loop_runs_cycle_when_enabled():
    stop_event = threading.Event()

    def fake_cycle(**kwargs):
        stop_event.set()
        return {"condition_ids": 0, "ingested": 0, "profiled": 0}

    with patch.object(wis, "WALLET_INTELLIGENCE_ENABLED", True), \
         patch.object(wis, "run_wallet_intelligence_cycle", side_effect=fake_cycle) as mock_cycle:
        wis.run_wallet_intelligence_loop(stop_event=stop_event)

    mock_cycle.assert_called_once()
