import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from analytics.market_scorer import normalize_market, score_market


def _raw_market(**overrides):
    base = {
        "question": "Will the US impose new tariffs on Chinese semiconductors by Q3 2026?",
        "category": "economics",
        "volume24hr": "85000",
        "endDate": "2099-01-01T00:00:00Z",  # far future, keeps days_remaining stable across test runs
        "bestBid": "0.33",
        "bestAsk": "0.37",
        "outcomePrices": '["0.35", "0.65"]',
        "closed": False,
    }
    base.update(overrides)
    return base


def _approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_normalize_market_extracts_expected_fields():
    normalized = normalize_market(_raw_market())
    assert normalized["category"] == "economics"
    assert normalized["volume_24h"] == 85000.0
    assert _approx(normalized["spread"], 0.04)
    assert _approx(normalized["price"], 0.35)
    assert normalized["days_remaining"] > 1000  # far-future endDate
    assert normalized["closed"] is False


def test_normalize_market_returns_none_when_question_missing():
    raw = _raw_market()
    del raw["question"]
    assert normalize_market(raw) is None


def test_score_market_skips_sports_category_immediately():
    market = normalize_market(_raw_market(
        question="Will Real Madrid win the Champions League 2026?",
        category="sports",
    ))
    assert score_market(market) == 0.0


def test_score_market_skips_unlisted_category():
    market = normalize_market(_raw_market(question="Will it rain tomorrow?", category="weather"))
    assert score_market(market) == 0.0


def test_score_market_skips_already_closed_market():
    market = normalize_market(_raw_market(closed=True))
    assert score_market(market) == 0.0


def test_score_market_worked_example_tariffs():
    # Matches reference/auto_discovery.md worked example: expected total 0.80
    market = normalize_market(_raw_market())
    market["days_remaining"] = 45.0  # override to match doc's example exactly
    assert _approx(score_market(market), 0.80)


def test_score_market_worked_example_bitcoin_caps_at_one():
    market = normalize_market(_raw_market(
        question="Will Bitcoin hit $200k in 2026?",
        category="crypto",
        volume24hr="2200000",
        bestBid="0.14",
        bestAsk="0.16",
        outcomePrices='["0.15", "0.85"]',
    ))
    market["days_remaining"] = 180.0
    assert score_market(market) == 1.0


# -- Error-case tests: malformed inputs gracefully fall back ----------------


def test_normalize_market_with_unparsable_enddate_returns_none():
    raw = _raw_market(endDate="not-a-date")
    assert normalize_market(raw) is None


def test_normalize_market_with_non_numeric_volume24hr_falls_back_to_zero():
    raw = _raw_market(volume24hr="not-a-number")
    normalized = normalize_market(raw)
    assert normalized is not None
    assert normalized["volume_24h"] == 0.0


def test_normalize_market_with_non_numeric_bestbid_falls_back_to_default_spread():
    raw = _raw_market(bestBid="not-a-number", bestAsk="0.37")
    normalized = normalize_market(raw)
    assert normalized is not None
    from analytics.market_scorer import DEFAULT_SPREAD
    assert _approx(normalized["spread"], DEFAULT_SPREAD)


def test_normalize_market_with_non_numeric_bestask_falls_back_to_default_spread():
    raw = _raw_market(bestBid="0.33", bestAsk="not-a-number")
    normalized = normalize_market(raw)
    assert normalized is not None
    from analytics.market_scorer import DEFAULT_SPREAD
    assert _approx(normalized["spread"], DEFAULT_SPREAD)


def test_normalize_market_with_missing_bestbid_falls_back_to_default_spread():
    raw = _raw_market()
    del raw["bestBid"]
    normalized = normalize_market(raw)
    assert normalized is not None
    from analytics.market_scorer import DEFAULT_SPREAD
    assert _approx(normalized["spread"], DEFAULT_SPREAD)


def test_normalize_market_with_unparsable_outcomePrices_falls_back_to_default_price():
    raw = _raw_market(outcomePrices="not-json")
    normalized = normalize_market(raw)
    assert normalized is not None
    from analytics.market_scorer import DEFAULT_PRICE
    assert _approx(normalized["price"], DEFAULT_PRICE)


def test_normalize_market_with_outcomePrices_empty_list_falls_back_to_default_price():
    raw = _raw_market(outcomePrices="[]")
    normalized = normalize_market(raw)
    assert normalized is not None
    from analytics.market_scorer import DEFAULT_PRICE
    assert _approx(normalized["price"], DEFAULT_PRICE)


# -- Tiering and selection tests -----------------------------------------------


def test_select_tiered_markets_tier1_always_included_uncapped():
    from analytics.market_scorer import select_tiered_markets, TIER1_THRESHOLD

    markets = [{"slug": f"tier1-{i}", "score": 0.9} for i in range(10)]
    selected = select_tiered_markets(markets, threshold=0.5, max_total=5)

    assert len(selected) == 10
    assert all(m["tier"] == 1 for m in selected)


def test_select_tiered_markets_fills_remaining_slots_with_best_tier2():
    from analytics.market_scorer import select_tiered_markets

    tier1 = [{"slug": "tier1-a", "score": 0.9}]
    tier2 = [
        {"slug": "tier2-low", "score": 0.55},
        {"slug": "tier2-mid", "score": 0.65},
        {"slug": "tier2-high", "score": 0.75},
    ]
    selected = select_tiered_markets(tier1 + tier2, threshold=0.5, max_total=3)

    slugs = {m["slug"] for m in selected}
    assert slugs == {"tier1-a", "tier2-high", "tier2-mid"}
    assert "tier2-low" not in slugs


def test_select_tiered_markets_excludes_below_threshold():
    from analytics.market_scorer import select_tiered_markets

    markets = [{"slug": "too-low", "score": 0.3}]
    selected = select_tiered_markets(markets, threshold=0.5, max_total=500)
    assert selected == []


def test_select_tiered_markets_score_exactly_at_tier1_threshold_is_tier2():
    from analytics.market_scorer import select_tiered_markets

    markets = [{"slug": "exactly-0.8", "score": 0.8}]
    selected = select_tiered_markets(markets, threshold=0.5, max_total=500)
    assert len(selected) == 1
    assert selected[0]["tier"] == 2


def test_select_tiered_markets_score_exactly_at_threshold_is_included():
    from analytics.market_scorer import select_tiered_markets

    markets = [{"slug": "exactly-threshold", "score": 0.5}]
    selected = select_tiered_markets(markets, threshold=0.5, max_total=500)
    assert len(selected) == 1
    assert selected[0]["tier"] == 2


def test_backfill_to_minimum_noop_when_already_at_target():
    from analytics.market_scorer import backfill_to_minimum

    selected = [{"slug": "a", "score": 0.9, "tier": 1}, {"slug": "b", "score": 0.6, "tier": 2}]
    result = backfill_to_minimum(selected, scored_markets=selected, min_total=2, max_total=500)
    assert result == selected


def test_backfill_to_minimum_pulls_highest_scoring_below_threshold_markets():
    from analytics.market_scorer import backfill_to_minimum, FLOOR_TIER

    selected = [{"slug": "a", "score": 0.9, "tier": 1}]
    scored = selected + [
        {"slug": "b", "score": 0.3},
        {"slug": "c", "score": 0.45},
        {"slug": "d", "score": 0.1},
    ]
    result = backfill_to_minimum(selected, scored, min_total=3, max_total=500)

    slugs = {m["slug"] for m in result}
    assert slugs == {"a", "c", "b"}  # highest two below-threshold scores, not "d"
    backfilled = {m["slug"]: m["tier"] for m in result if m["slug"] != "a"}
    assert backfilled == {"c": FLOOR_TIER, "b": FLOOR_TIER}


def test_backfill_to_minimum_excludes_zero_score_markets():
    from analytics.market_scorer import backfill_to_minimum

    selected = []
    scored = [{"slug": "skip-me", "score": 0.0}]
    result = backfill_to_minimum(selected, scored, min_total=5, max_total=500)
    assert result == []


def test_backfill_to_minimum_never_exceeds_max_total():
    from analytics.market_scorer import backfill_to_minimum

    selected = [{"slug": "a", "score": 0.9, "tier": 1}]
    scored = selected + [{"slug": f"cand-{i}", "score": 0.4} for i in range(10)]
    result = backfill_to_minimum(selected, scored, min_total=100, max_total=3)
    assert len(result) == 3
