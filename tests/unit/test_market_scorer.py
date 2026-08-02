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
