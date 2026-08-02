import sys
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import core.auto_discovery as auto_discovery


def test_check_disk_usage_computes_percentage():
    with patch("core.auto_discovery.shutil.disk_usage", return_value=(100 * 1024**3, 70 * 1024**3, 30 * 1024**3)):
        result = auto_discovery.check_disk_usage()
    assert result["used_pct"] == 70.0
    assert result["used_gb"] == 70.0
    assert result["total_gb"] == 100.0


def test_disk_gate_level_boundaries():
    assert auto_discovery.disk_gate_level(69.9) == "normal"
    assert auto_discovery.disk_gate_level(70.0) == "warning"
    assert auto_discovery.disk_gate_level(80.0) == "alert"
    assert auto_discovery.disk_gate_level(90.0) == "critical"


def test_diff_discovery_cycle_detects_new_market():
    result = auto_discovery.diff_discovery_cycle({"a", "b"}, {})
    assert result["new_slugs"] == {"a", "b"}
    assert result["kept_slugs"] == set()
    assert result["resolved_slugs"] == set()


def test_diff_discovery_cycle_kept_market_has_no_miss():
    result = auto_discovery.diff_discovery_cycle({"a"}, {"a": 1})
    assert result["kept_slugs"] == {"a"}
    assert result["new_slugs"] == set()


def test_diff_discovery_cycle_first_miss_not_resolved():
    result = auto_discovery.diff_discovery_cycle(set(), {"a": 0})
    assert result["missed_slugs"] == {"a": 1}
    assert result["resolved_slugs"] == set()


def test_diff_discovery_cycle_second_miss_resolved():
    result = auto_discovery.diff_discovery_cycle(set(), {"a": 1})
    assert result["resolved_slugs"] == {"a"}
    assert result["missed_slugs"] == {}


import time as _time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import AutoSubscription


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


_TARIFF_MARKET = {
    "question": "Will the US impose new tariffs on Chinese semiconductors by Q3 2026?",
    "category": "economics",
    "volume24hr": "85000",
    "endDate": "2099-01-01T00:00:00Z",
    "bestBid": "0.33",
    "bestAsk": "0.37",
    "outcomePrices": '["0.35", "0.65"]',
    "closed": False,
    "slug": "us-china-tariffs-q3-2026",
    "token_ids": ["tok_1", "tok_2"],
}


def test_extract_token_ids_handles_json_string_and_list():
    assert auto_discovery._extract_token_ids({"clobTokenIds": '["a", "b"]'}) == ["a", "b"]
    assert auto_discovery._extract_token_ids({"clobTokenIds": ["c", "d"]}) == ["c", "d"]
    assert auto_discovery._extract_token_ids({}) == []


def test_fetch_candidate_markets_dedupes_by_slug():
    volume_response = [{"slug": "market-a", "markets": [{"clobTokenIds": '["t1"]'}]}]
    recent_response = [{"slug": "market-a", "markets": [{"clobTokenIds": '["t1"]'}]},
                        {"slug": "market-b", "markets": [{"clobTokenIds": '["t2"]'}]}]

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        if params["order"] == "volume24Hr":
            return FakeResponse(volume_response)
        return FakeResponse(recent_response)

    with patch("core.auto_discovery.requests.get", side_effect=fake_get):
        candidates = auto_discovery.fetch_candidate_markets()

    slugs = {c["slug"] for c in candidates}
    assert slugs == {"market-a", "market-b"}


def test_run_discovery_cycle_subscribes_new_qualifying_market():
    session_factory = _sqlite_session_factory()
    subscribed = []

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[dict(_TARIFF_MARKET)]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append((slug, tids)),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert subscribed == [("us-china-tariffs-q3-2026", ["tok_1", "tok_2"])]
    assert summary["new"] == 1
    assert summary["active"] == 1

    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="us-china-tariffs-q3-2026").first()
    assert row.status == "active"
    db.close()


def test_run_discovery_cycle_marks_resolved_after_two_consecutive_misses():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="old-market", question="Old?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_x"], subscribed_at=_time.time(),
        last_seen_active=_time.time(), last_cycle_at=_time.time(),
        status="active", consecutive_misses=1,
    ))
    db.commit()
    db.close()

    unsubscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: unsubscribed.append(slug),
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert unsubscribed == ["old-market"]
    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="old-market").first()
    assert row.status == "resolved"
    db.close()


def test_run_discovery_cycle_blocks_new_tier2_but_not_tier1_when_disk_warning():
    session_factory = _sqlite_session_factory()
    tier2_market = {
        "question": "Will a Bitcoin ETF get approved?", "category": "crypto",
        "volume24hr": "12000", "endDate": "2099-01-01T00:00:00Z",
        "bestBid": "0.40", "bestAsk": "0.45", "outcomePrices": '["0.42", "0.58"]',
        "closed": False, "slug": "btc-etf-approval", "token_ids": ["tok_a"],
    }

    subscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[tier2_market]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 75.0, "used_gb": 75.0, "total_gb": 100.0}):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append(slug),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert subscribed == []
    assert summary["new"] == 0


def test_run_discovery_cycle_tier1_unblocked_and_tier2_blocked_same_cycle_disk_warning():
    session_factory = _sqlite_session_factory()
    tier1_market = dict(_TARIFF_MARKET)  # score > 0.8 (high volume, long time horizon)
    tier2_market = {
        "question": "Will a Bitcoin ETF get approved?", "category": "crypto",
        "volume24hr": "12000", "endDate": "2099-01-01T00:00:00Z",
        "bestBid": "0.40", "bestAsk": "0.45", "outcomePrices": '["0.42", "0.58"]',
        "closed": False, "slug": "btc-etf-approval", "token_ids": ["tok_a"],
    }

    subscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[tier1_market, tier2_market]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 75.0, "used_gb": 75.0, "total_gb": 100.0}):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append(slug),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert subscribed == ["us-china-tariffs-q3-2026"]
    assert summary["new"] == 1


def test_run_discovery_cycle_sleeps_once_per_new_market():
    session_factory = _sqlite_session_factory()
    markets = []
    for i in range(3):
        m = dict(_TARIFF_MARKET)
        m["slug"] = f"market-{i}"
        markets.append(m)

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=markets), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}), \
         patch("core.auto_discovery.time.sleep") as mock_sleep:
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert mock_sleep.call_count == 3


def test_run_discovery_cycle_no_db_row_when_subscribe_callback_raises():
    session_factory = _sqlite_session_factory()
    good_market = dict(_TARIFF_MARKET)
    bad_market = dict(_TARIFF_MARKET)
    bad_market["slug"] = "bad-market"

    def flaky_subscribe(slug, tids):
        if slug == "bad-market":
            raise RuntimeError("GlobalWebSocketManager wiring failed")

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[good_market, bad_market]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=flaky_subscribe,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert summary["new"] == 1

    db = session_factory()
    good_row = db.query(AutoSubscription).filter_by(slug="us-china-tariffs-q3-2026").first()
    bad_row = db.query(AutoSubscription).filter_by(slug="bad-market").first()
    db.close()

    assert good_row is not None
    assert good_row.status == "active"
    assert bad_row is None


def test_run_discovery_cycle_stays_active_when_unsubscribe_callback_raises():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="flaky-unsub-market", question="Old?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_x"], subscribed_at=_time.time(),
        last_seen_active=_time.time(), last_cycle_at=_time.time(),
        status="active", consecutive_misses=1,
    ))
    db.commit()
    db.close()

    def flaky_unsubscribe(slug):
        raise RuntimeError("unsubscribe failed")

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=flaky_unsubscribe,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="flaky-unsub-market").first()
    db.close()

    assert row.status == "active"


import threading


def test_restore_active_subscriptions_uses_stored_token_ids_with_zero_api_calls():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="stored-market", question="Q?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_stored"], subscribed_at=_time.time(),
        status="active",
    ))
    db.commit()
    db.close()

    restored_calls = []
    with patch("core.auto_discovery.requests.get") as mock_get:
        count = auto_discovery.restore_active_subscriptions(
            subscribe_callback=lambda slug, tids: restored_calls.append((slug, tids)),
            session_factory=session_factory,
        )

    mock_get.assert_not_called()
    assert count == 1
    assert restored_calls == [("stored-market", ["tok_stored"])]


def test_run_scheduler_loop_restores_then_runs_one_cycle_then_stops():
    stop_event = threading.Event()
    cycle_calls = []

    def fake_cycle(subscribe_callback, unsubscribe_callback, alert_callback, redis_client):
        cycle_calls.append(1)
        stop_event.set()
        return {"new": 0, "resolved": 0, "active": 0, "tier1": 0, "tier2": 0, "disk_used_pct": 0.0}

    with patch.object(auto_discovery, "restore_active_subscriptions", return_value=0), \
         patch.object(auto_discovery, "run_discovery_cycle", side_effect=fake_cycle):
        auto_discovery.run_scheduler_loop(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            stop_event=stop_event,
        )

    assert cycle_calls == [1]
