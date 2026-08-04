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
    sql = wis.build_wallet_intelligence_query(["0xABC123", "0xdef456"], lookback_days=2, min_usd=100, row_limit=5000)
    # Lowercased regardless of input casing -- Dune's to_hex(varbinary)
    # returns uppercase hex, so the comparison must be case-insensitive.
    assert "'0xabc123'" in sql
    assert "'0xdef456'" in sql
    assert "lower(to_hex(t.condition_id))" in sql
    assert "t.amount >= 100" in sql
    assert "LIMIT 5000" in sql
    assert "t.is_taker_side = TRUE" in sql
    assert "t.action = 'CLOB trade'" in sql


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


def test_run_wallet_intelligence_loop_disabled_does_nothing():
    """Explicitly forces the disabled state rather than relying on the
    ambient WALLET_INTELLIGENCE_ENABLED default -- a real .env can (and, on
    this dev box, does) set it to true, which would otherwise make this
    test silently fire a real Dune query and block for a real 6-24h wait.
    """
    with patch.object(wis, "WALLET_INTELLIGENCE_ENABLED", False), \
         patch.object(wis, "run_wallet_intelligence_cycle") as mock_cycle:
        wis.run_wallet_intelligence_loop()

    mock_cycle.assert_not_called()


def test_ingest_rows_enriches_existing_sparse_row():
    """A row the Step 9 live monitor inserted first (category=None) must get
    enriched, not permanently skipped, when Dune later re-sees the same
    blockchain_id.
    """
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(OnchainTrade(
        blockchain_id="AAAA-0", wallet_address="0xabc", market_id="0xmkt",
        question=None, outcome=None, category=None,
        usd_volume=5.0, entry_price=0.5, block_timestamp=time.time(),
    ))
    db.commit()
    db.close()

    fake_dune = MagicMock()
    fake_dune.fetch_results_paginated.return_value = iter([
        {
            "blockchain_id": "AAAA-0", "wallet_address": "0xabc", "market_id": "0xmkt",
            "event_market_name": "Fed rate cut", "question": "Will the Fed cut rates?",
            "outcome": "Yes", "usd_volume": 5.0, "entry_price": 0.5,
            "block_timestamp": time.time(),
        },
    ])

    fake_resolution_client = MagicMock()
    fake_resolution_client.resolve_market.return_value = wis.MarketResolution(
        resolved_outcome="Yes", market_end_time=1999999999.0
    )

    db = session_factory()
    ingested = wis._ingest_rows(db, fake_dune, "exec-1", fake_resolution_client)
    db.close()

    assert ingested == 0  # not a new row -- enrichment doesn't count as "ingested"

    db = session_factory()
    row = db.query(OnchainTrade).filter_by(blockchain_id="AAAA-0").first()
    assert row.category is not None
    assert row.question == "Will the Fed cut rates?"
    assert row.outcome == "Yes"
    assert row.resolved_outcome == "Yes"
    assert row.market_end_time == 1999999999.0
    db.close()


def test_ingest_rows_does_not_re_enrich_already_categorized_row():
    """A row that already has a category (came from a prior Dune ingestion,
    not the live monitor) must not be touched again -- category is set once.
    """
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(OnchainTrade(
        blockchain_id="BBBB-0", wallet_address="0xabc", market_id="0xmkt",
        question="Original question", outcome="No", category="fed",
        usd_volume=5.0, entry_price=0.5, block_timestamp=time.time(),
    ))
    db.commit()
    db.close()

    fake_dune = MagicMock()
    fake_dune.fetch_results_paginated.return_value = iter([
        {
            "blockchain_id": "BBBB-0", "wallet_address": "0xabc", "market_id": "0xmkt",
            "event_market_name": "Different event", "question": "A different question?",
            "outcome": "Yes", "usd_volume": 5.0, "entry_price": 0.5,
            "block_timestamp": time.time(),
        },
    ])
    fake_resolution_client = MagicMock()

    db = session_factory()
    wis._ingest_rows(db, fake_dune, "exec-1", fake_resolution_client)
    db.close()

    fake_resolution_client.resolve_market.assert_not_called()
    db = session_factory()
    row = db.query(OnchainTrade).filter_by(blockchain_id="BBBB-0").first()
    assert row.question == "Original question"
    assert row.category == "fed"
    db.close()


def test_run_wallet_intelligence_loop_runs_cycle_when_enabled():
    stop_event = threading.Event()

    def fake_cycle(**kwargs):
        stop_event.set()
        return {"condition_ids": 0, "ingested": 0, "profiled": 0}

    with patch.object(wis, "WALLET_INTELLIGENCE_ENABLED", True), \
         patch.object(wis, "run_wallet_intelligence_cycle", side_effect=fake_cycle) as mock_cycle:
        wis.run_wallet_intelligence_loop(stop_event=stop_event)

    mock_cycle.assert_called_once()
