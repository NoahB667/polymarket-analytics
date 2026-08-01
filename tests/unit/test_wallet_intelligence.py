import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from analytics.wallet_intelligence import (
    classify_category,
    compile_profile,
    calculate_insider_score,
)
from models.dataclasses import WalletProfile


def _trade(
    market_id="mkt-1",
    category="fed",
    entry_price=0.10,
    outcome="Yes",
    resolved_outcome=None,
    usd_volume=1000.0,
    block_timestamp=None,
    market_end_time=None,
):
    return {
        "market_id": market_id,
        "category": category,
        "entry_price": entry_price,
        "outcome": outcome,
        "resolved_outcome": resolved_outcome,
        "usd_volume": usd_volume,
        "block_timestamp": block_timestamp if block_timestamp is not None else time.time(),
        "market_end_time": market_end_time,
    }


def test_classify_category_matches_keyword():
    assert classify_category("Will the Fed cut rates in June?") == "fed"
    assert classify_category("Random sports market") == "other"
    assert classify_category(None) == "other"


def test_compile_profile_empty_trades_returns_zeroed_profile():
    profile = compile_profile("0xabc", [])
    assert profile.total_trades == 0
    assert profile.unique_markets == 0
    assert profile.longshot_attempts == 0
    assert profile.category_concentration == 0.0
    assert profile.new_account_flag is False


def test_compile_profile_basic_aggregation():
    trades = [
        _trade(market_id="a", usd_volume=1000.0),
        _trade(market_id="b", usd_volume=3000.0),
    ]
    profile = compile_profile("0xabc", trades)
    assert profile.total_trades == 2
    assert profile.unique_markets == 2
    assert profile.avg_bet_size == 2000.0


def test_compile_profile_longshot_win_rate_uses_resolution():
    now = time.time()
    trades = [
        _trade(entry_price=0.10, outcome="Yes", resolved_outcome="Yes", block_timestamp=now),
        _trade(entry_price=0.15, outcome="Yes", resolved_outcome="No", block_timestamp=now),
        _trade(entry_price=0.05, outcome="No", resolved_outcome="No", block_timestamp=now),
        _trade(entry_price=0.30, outcome="Yes", resolved_outcome=None, block_timestamp=now),  # not a long shot
        _trade(entry_price=0.18, outcome="Yes", resolved_outcome=None, block_timestamp=now),  # unresolved long shot
    ]
    profile = compile_profile("0xabc", trades)
    assert profile.longshot_attempts == 4  # entry_price < 0.20
    assert profile.longshot_wins == 2
    assert profile.longshot_win_rate == 0.5
    assert profile.avg_implied_prob_at_entry == round((0.10 + 0.15 + 0.05 + 0.18) / 4, 4)


def test_compile_profile_category_concentration_and_top_categories():
    trades = [
        _trade(category="fed"),
        _trade(category="fed"),
        _trade(category="fed"),
        _trade(category="crypto"),
    ]
    profile = compile_profile("0xabc", trades)
    assert profile.category_concentration == 0.75
    assert profile.top_categories[0] == "fed"


def test_compile_profile_resolution_proximity_averages_only_known_gaps():
    now = time.time()
    trades = [
        _trade(block_timestamp=now, market_end_time=now + 86400 * 2),  # 2 days before resolution
        _trade(block_timestamp=now, market_end_time=None),             # unknown, excluded from average
    ]
    profile = compile_profile("0xabc", trades)
    assert profile.avg_days_before_resolution == 2.0


def test_compile_profile_resolution_proximity_defaults_when_unknown():
    trades = [_trade(market_end_time=None)]
    profile = compile_profile("0xabc", trades)
    assert profile.avg_days_before_resolution > 100  # sentinel: no resolution timing data


def test_compile_profile_new_account_flag():
    recent = time.time() - (10 * 86400)   # 10 days old
    old = time.time() - (365 * 86400)     # 1 year old
    assert compile_profile("0xabc", [_trade(block_timestamp=recent)]).new_account_flag is True
    assert compile_profile("0xabc", [_trade(block_timestamp=old)]).new_account_flag is False


def test_calculate_insider_score_gates_on_minimum_longshot_sample():
    profile = compile_profile("0xabc", [
        _trade(entry_price=0.05, outcome="Yes", resolved_outcome="Yes"),
        _trade(entry_price=0.05, outcome="Yes", resolved_outcome="Yes"),
    ])
    score = calculate_insider_score(profile)
    assert profile.score_components["longshot_excess"] == 0.0
    assert score == profile.insider_score


def test_calculate_insider_score_longshot_excess_component():
    now = time.time()
    trades = [_trade(entry_price=0.10, outcome="Yes", resolved_outcome="Yes", block_timestamp=now)
              for _ in range(6)]
    profile = compile_profile("0xabc", trades)
    score = calculate_insider_score(profile)
    # win_rate=1.0, baseline avg_implied_prob=0.10 -> excess=0.90 -> capped at 0.40
    assert profile.score_components["longshot_excess"] == 0.40
    assert score >= 0.40


def test_calculate_insider_score_new_account_large_bet_component():
    recent = time.time() - (5 * 86400)
    profile = compile_profile("0xabc", [_trade(block_timestamp=recent, usd_volume=5000.0)])
    score = calculate_insider_score(profile)
    assert profile.score_components["new_account_large_bet"] == 0.20


def test_calculate_insider_score_few_trades_high_winrate_component():
    now = time.time()
    trades = [_trade(entry_price=0.10, outcome="Yes", resolved_outcome="Yes", block_timestamp=now)
              for _ in range(5)]
    profile = compile_profile("0xabc", trades)
    score = calculate_insider_score(profile)
    assert profile.score_components["few_trades_high_winrate"] == 0.10


def test_calculate_insider_score_clamped_to_one():
    recent = time.time() - (1 * 86400)
    now = time.time()
    trades = [
        _trade(entry_price=0.05, outcome="Yes", resolved_outcome="Yes",
               category="fed", usd_volume=5000.0, block_timestamp=recent,
               market_end_time=recent + 3600)
        for _ in range(6)
    ]
    profile = compile_profile("0xabc", trades)
    score = calculate_insider_score(profile)
    assert 0.0 <= score <= 1.0
    assert score == 1.0
