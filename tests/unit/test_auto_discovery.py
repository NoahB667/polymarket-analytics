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
from models.orm import AutoSubscription, Trade


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_update_total_trades_collected_counts_per_market():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="market-a", question="Q?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_a"], subscribed_at=_time.time(), status="active",
    ))
    db.add(AutoSubscription(
        slug="market-b", question="Q?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_b"], subscribed_at=_time.time(), status="resolved",
    ))
    for _ in range(3):
        db.add(Trade(slug="market-a", price=0.5, size=1.0, usd=0.5, side="BUY", timestamp=_time.time()))
    db.add(Trade(slug="market-b", price=0.5, size=1.0, usd=0.5, side="BUY", timestamp=_time.time()))
    db.commit()
    db.close()

    db = session_factory()
    auto_discovery._update_total_trades_collected(db)

    row_a = db.query(AutoSubscription).filter_by(slug="market-a").first()
    row_b = db.query(AutoSubscription).filter_by(slug="market-b").first()
    assert row_a.total_trades_collected == 3
    # market-b is resolved (not active) -- its count is not refreshed, stays 0
    assert row_b.total_trades_collected == 0
    db.close()


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


def test_extract_category_joins_tag_labels():
    event = {
        "tags": [
            {"label": "Sports", "slug": "sports"},
            {"label": "MLB", "slug": "mlb"},
            {"label": "baseball", "slug": "baseball"},
        ]
    }
    assert auto_discovery._extract_category(event) == "Sports, MLB, baseball"


def test_extract_category_returns_empty_string_when_no_tags():
    assert auto_discovery._extract_category({}) == ""
    assert auto_discovery._extract_category({"tags": []}) == ""
    assert auto_discovery._extract_category({"tags": None}) == ""


def test_extract_category_truncates_to_column_width():
    """Regression test: a market with many tags produced a category string
    that exceeded AutoSubscription.category's VARCHAR width and rolled back
    an entire cycle's bulk insert in production.
    """
    event = {"tags": [{"label": f"tag-{i}-with-a-fairly-long-label"} for i in range(50)]}
    result = auto_discovery._extract_category(event)
    assert len(result) == auto_discovery.CATEGORY_MAX_LENGTH


def test_merge_events_page_uses_event_tags_not_market_category():
    """Regression test: Gamma's live API never populates market.category or
    event.category (both always None) -- the real signal is event.tags.
    Without pulling from tags, a sports market with a question that doesn't
    literally say "sports"/"mlb" would slip past the category hard filter.
    """
    events = {}
    raw_events = [{
        "slug": "mlb-world-series-champion-2026",
        "category": None,
        "tags": [{"label": "Sports"}, {"label": "MLB"}, {"label": "baseball"}],
        "markets": [{
            "question": "Who will win the World Series?",
            "category": None,
            "clobTokenIds": '["t1", "t2"]',
        }],
    }]

    auto_discovery._merge_events_page(events, raw_events)

    assert events["mlb-world-series-champion-2026"]["category"] == "Sports, MLB, baseball"


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
        if params["order"] == "volume24hr":
            return FakeResponse(volume_response)
        return FakeResponse(recent_response)

    with patch("core.auto_discovery.requests.get", side_effect=fake_get):
        candidates = auto_discovery.fetch_candidate_markets()

    slugs = {c["slug"] for c in candidates}
    assert slugs == {"market-a", "market-b"}


def test_fetch_candidate_markets_paginates_top_volume_source():
    """Verify multiple full pages are fetched via offset, stopping on a short page."""
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    def _volume_page(offset, count):
        return [
            {"slug": f"vol-{offset + i}", "markets": [{"clobTokenIds": '["t"]'}]}
            for i in range(count)
        ]

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params))
        if params["order"] == "volume24hr":
            offset = params.get("offset", 0)
            if offset == 0:
                return FakeResponse(_volume_page(0, auto_discovery.TOP_VOLUME_PAGE_SIZE))
            if offset == auto_discovery.TOP_VOLUME_PAGE_SIZE:
                return FakeResponse(_volume_page(offset, 10))  # short page -- stop here
            raise AssertionError("should not fetch a third page after a short page")
        return FakeResponse([])

    with patch("core.auto_discovery.requests.get", side_effect=fake_get):
        candidates = auto_discovery.fetch_candidate_markets()

    volume_calls = [c for c in calls if c["order"] == "volume24hr"]
    assert len(volume_calls) == 2  # full page then short page, then stop
    assert len(candidates) == auto_discovery.TOP_VOLUME_PAGE_SIZE + 10


class FakeRedis:
    """Minimal in-memory Redis stand-in supporting setex/pipeline(delete+hset+expire).

    Just enough surface area to assert on final key/value state after
    run_discovery_cycle's metadata pre-warm -- not a general-purpose fake.
    """

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.strings[key] = value
        self.ttls[key] = ttl

    def set(self, key, value):
        self.strings[key] = value

    def incr(self, key):
        self.strings[key] = self.strings.get(key, 0) + 1

    def pipeline(self):
        return _FakeRedisPipeline(self)


class _FakeRedisPipeline:
    def __init__(self, parent: FakeRedis):
        self._parent = parent
        self._ops = []

    def delete(self, key):
        self._ops.append(("delete", key))
        return self

    def hset(self, key, mapping):
        self._ops.append(("hset", key, mapping))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "delete":
                self._parent.hashes.pop(op[1], None)
                results.append(1)
            elif op[0] == "hset":
                self._parent.hashes.setdefault(op[1], {}).update(op[2])
                results.append(len(op[2]))
            elif op[0] == "expire":
                self._parent.ttls[op[1]] = op[2]
                results.append(True)
        self._ops = []
        return results


_TARIFF_MARKET_WITH_CONDITION_ID = {
    **_TARIFF_MARKET,
    "conditionId": "0xabc123def456",
    "outcomes": '["Yes", "No"]',
}


def test_run_discovery_cycle_prewarms_metadata_keyed_on_market_id_with_outcomes_hash():
    session_factory = _sqlite_session_factory()
    fake_redis = FakeRedis()

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[dict(_TARIFF_MARKET_WITH_CONDITION_ID)]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            redis_client=fake_redis,
            session_factory=session_factory,
        )

    market_id = "0xabc123def456"
    slug = "us-china-tariffs-q3-2026"

    # Written under the market id, not the slug.
    assert fake_redis.strings[f"meta:question:{market_id}"] == _TARIFF_MARKET["question"]
    assert f"meta:question:{slug}" not in fake_redis.strings

    # Outcomes hash maps token_id -> outcome label, keyed on market id.
    assert fake_redis.hashes[f"meta:outcomes:{market_id}"] == {"tok_1": "Yes", "tok_2": "No"}
    assert fake_redis.ttls[f"meta:question:{market_id}"] == 86400
    assert fake_redis.ttls[f"meta:outcomes:{market_id}"] == 86400


def test_run_discovery_cycle_persists_condition_id():
    """condition_id must be stored so the Dune wallet-intelligence backfill
    can later filter on-chain trades by market without a slug->id lookup.
    """
    session_factory = _sqlite_session_factory()

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[dict(_TARIFF_MARKET_WITH_CONDITION_ID)]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="us-china-tariffs-q3-2026").first()
    assert row.condition_id == "0xabc123def456"
    db.close()


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


def test_run_discovery_cycle_reactivates_previously_resolved_slug_instead_of_duplicate_insert():
    """Regression: a market marked 'resolved' (e.g. it missed selection
    twice transiently, per RESOLVED_MISS_THRESHOLD) that reappears as a
    qualifying Gamma candidate must be reactivated in place, not
    re-INSERTed. A fresh INSERT collides with the slug's unique constraint
    on commit, which rolls back the ENTIRE cycle's transaction -- silently
    discarding every other legitimate update (other new markets,
    last_cycle_at/consecutive_misses refreshes for already-active markets)
    made in that same cycle, even though the cycle logs as "complete".
    """
    original_subscribed_at = _time.time() - 100000
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="us-china-tariffs-q3-2026", question="stale", category="economics",
        market_score=0.5, tier=2, volume_24h=100.0, days_remaining=1.0,
        token_ids=["stale_tok"], subscribed_at=original_subscribed_at,
        last_seen_active=original_subscribed_at, last_cycle_at=original_subscribed_at,
        status="resolved", consecutive_misses=2,
    ))
    # A second, genuinely-active market whose bookkeeping update must
    # survive the cycle -- this is what proves the whole-cycle rollback is
    # actually fixed, not just the collision itself.
    db.add(AutoSubscription(
        slug="already-active-market", question="Q", category="c",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_x"], subscribed_at=_time.time() - 1000,
        last_seen_active=_time.time() - 1000, last_cycle_at=_time.time() - 1000,
        status="active", consecutive_misses=0,
    ))
    db.commit()
    db.close()

    subscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[dict(_TARIFF_MARKET)]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append((slug, tids)),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert subscribed == [("us-china-tariffs-q3-2026", ["tok_1", "tok_2"])]

    db = session_factory()
    reactivated = db.query(AutoSubscription).filter_by(slug="us-china-tariffs-q3-2026").all()
    assert len(reactivated) == 1  # never a duplicate row
    row = reactivated[0]
    assert row.status == "active"
    assert row.token_ids == ["tok_1", "tok_2"]
    assert row.consecutive_misses == 0
    assert row.subscribed_at == original_subscribed_at  # first-discovery time preserved, not reset

    # The unrelated already-active market wasn't in this cycle's candidates
    # (fetch_candidate_markets only returned the tariff market), so it
    # should have picked up a consecutive_misses bump -- proving this
    # cycle's OTHER updates weren't discarded by the earlier collision.
    other = db.query(AutoSubscription).filter_by(slug="already-active-market").first()
    assert other.consecutive_misses == 1
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


def test_run_discovery_cycle_self_heals_missing_condition_id_on_kept_market():
    """Rows created before the condition_id column existed have it backfilled
    for free the next time the market is re-fetched and kept active, using
    the fresh conditionId already present in this cycle's Gamma data.
    """
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="us-china-tariffs-q3-2026", question="Old?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_1", "tok_2"], subscribed_at=_time.time(),
        last_seen_active=_time.time(), last_cycle_at=_time.time(),
        status="active", consecutive_misses=0, condition_id=None,
    ))
    db.commit()
    db.close()

    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[dict(_TARIFF_MARKET_WITH_CONDITION_ID)]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}):
        auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="us-china-tariffs-q3-2026").first()
    assert row.condition_id == "0xabc123def456"
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


def test_run_discovery_cycle_backfills_to_min_active_markets_when_disk_normal():
    session_factory = _sqlite_session_factory()
    tier1_market = dict(_TARIFF_MARKET)  # scores > 0.8

    # Category-relevant (economics) but scores well below AUTO_DISCOVERY_THRESHOLD
    # (0.5): base 0.4 + tiny volume/time/spread bumps only.
    low_score_markets = []
    for i in range(3):
        m = {
            "question": f"Will some economic indicator {i} happen?", "category": "economics",
            "volume24hr": "500", "endDate": "2026-08-10T00:00:00Z",
            "bestBid": "0.40", "bestAsk": "0.60", "outcomePrices": '["0.5", "0.5"]',
            "closed": False, "slug": f"low-score-econ-{i}", "token_ids": [f"tok_low_{i}"],
        }
        low_score_markets.append(m)

    subscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[tier1_market] + low_score_markets), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 10.0, "used_gb": 1.0, "total_gb": 100.0}), \
         patch.object(auto_discovery, "MIN_ACTIVE_MARKETS", 4):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append(slug),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    # Tier 1 market plus all 3 low-scoring markets backfilled to reach the floor of 4.
    assert set(subscribed) == {"us-china-tariffs-q3-2026", "low-score-econ-0", "low-score-econ-1", "low-score-econ-2"}
    assert summary["tier3"] == 3

    db = session_factory()
    for i in range(3):
        row = db.query(AutoSubscription).filter_by(slug=f"low-score-econ-{i}").first()
        assert row.tier == 3
    db.close()


def test_run_discovery_cycle_does_not_backfill_when_disk_not_normal():
    session_factory = _sqlite_session_factory()
    low_score_market = {
        "question": "Will some economic indicator happen?", "category": "economics",
        "volume24hr": "500", "endDate": "2026-08-10T00:00:00Z",
        "bestBid": "0.40", "bestAsk": "0.60", "outcomePrices": '["0.5", "0.5"]',
        "closed": False, "slug": "low-score-econ", "token_ids": ["tok_low"],
    }

    subscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[low_score_market]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 75.0, "used_gb": 75.0, "total_gb": 100.0}), \
         patch.object(auto_discovery, "MIN_ACTIVE_MARKETS", 50):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: subscribed.append(slug),
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert subscribed == []
    assert summary["tier3"] == 0


def test_run_discovery_cycle_keeps_existing_tier2_market_active_under_disk_pressure():
    """Regression test: disk gating must only block NEW Tier 2 subscriptions.

    An already-active Tier 2 market that still qualifies this cycle must be
    treated as "kept" (consecutive_misses reset to 0, still active) even
    when the disk gate is non-normal -- not as "missing", which would
    eventually resolve/unsubscribe it purely due to transient disk pressure
    that has nothing to do with whether the market itself is still relevant.
    """
    session_factory = _sqlite_session_factory()
    tier2_market = {
        "question": "Will a Bitcoin ETF get approved?", "category": "crypto",
        "volume24hr": "12000", "endDate": "2099-01-01T00:00:00Z",
        "bestBid": "0.40", "bestAsk": "0.45", "outcomePrices": '["0.42", "0.58"]',
        "closed": False, "slug": "btc-etf-approval", "token_ids": ["tok_a"],
    }
    db = session_factory()
    db.add(AutoSubscription(
        slug="btc-etf-approval", question="Will a Bitcoin ETF get approved?", category="crypto",
        market_score=0.75, tier=2, volume_24h=12000.0, days_remaining=1000.0,
        token_ids=["tok_a"], subscribed_at=_time.time(),
        last_seen_active=_time.time(), last_cycle_at=_time.time(),
        status="active", consecutive_misses=1,  # one prior miss -- must NOT become 2/resolved
    ))
    db.commit()
    db.close()

    unsubscribed = []
    with patch.object(auto_discovery, "fetch_candidate_markets", return_value=[tier2_market]), \
         patch.object(auto_discovery, "check_disk_usage", return_value={"used_pct": 75.0, "used_gb": 75.0, "total_gb": 100.0}):
        summary = auto_discovery.run_discovery_cycle(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: unsubscribed.append(slug),
            alert_callback=lambda msg: None,
            session_factory=session_factory,
        )

    assert unsubscribed == []
    assert summary["resolved"] == 0

    db = session_factory()
    row = db.query(AutoSubscription).filter_by(slug="btc-etf-approval").first()
    assert row.status == "active"
    assert row.consecutive_misses == 0  # reset, not incremented -- this is "kept", not "missed"
    db.close()


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


def test_restore_active_subscriptions_isolates_per_row_failure():
    session_factory = _sqlite_session_factory()
    db = session_factory()
    db.add(AutoSubscription(
        slug="bad-market", question="Q?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_bad"], subscribed_at=_time.time(),
        status="active",
    ))
    db.add(AutoSubscription(
        slug="good-market", question="Q?", category="economics",
        market_score=0.9, tier=1, volume_24h=1000.0, days_remaining=10.0,
        token_ids=["tok_good"], subscribed_at=_time.time(),
        status="active",
    ))
    db.commit()
    db.close()

    restored_calls = []

    def flaky_subscribe(slug, tids):
        if slug == "bad-market":
            raise RuntimeError("subscribe wiring failed")
        restored_calls.append((slug, tids))

    count = auto_discovery.restore_active_subscriptions(
        subscribe_callback=flaky_subscribe,
        session_factory=session_factory,
    )

    assert ("good-market", ["tok_good"]) in restored_calls
    assert count == 1


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


def test_run_scheduler_loop_survives_cycle_exception_and_continues():
    stop_event = threading.Event()
    cycle_calls = []

    def flaky_cycle(subscribe_callback, unsubscribe_callback, alert_callback, redis_client):
        cycle_calls.append(1)
        if len(cycle_calls) == 1:
            raise RuntimeError("Gamma API outage")
        stop_event.set()
        return {"new": 0, "resolved": 0, "active": 0, "tier1": 0, "tier2": 0, "disk_used_pct": 0.0}

    with patch.object(auto_discovery, "restore_active_subscriptions", return_value=0), \
         patch.object(auto_discovery, "run_discovery_cycle", side_effect=flaky_cycle), \
         patch.object(auto_discovery, "AUTO_DISCOVERY_INTERVAL_HOURS", 0.0):
        auto_discovery.run_scheduler_loop(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
            stop_event=stop_event,
        )

    assert len(cycle_calls) >= 2


def test_run_scheduler_loop_disabled_returns_immediately_without_restore_or_cycle():
    restore_calls = []
    cycle_calls = []

    with patch.object(auto_discovery, "AUTO_DISCOVERY_ENABLED", False), \
         patch.object(auto_discovery, "restore_active_subscriptions", side_effect=lambda *a, **k: restore_calls.append(1)), \
         patch.object(auto_discovery, "run_discovery_cycle", side_effect=lambda *a, **k: cycle_calls.append(1)):
        auto_discovery.run_scheduler_loop(
            subscribe_callback=lambda slug, tids: None,
            unsubscribe_callback=lambda slug: None,
            alert_callback=lambda msg: None,
        )

    assert restore_calls == []
    assert cycle_calls == []
