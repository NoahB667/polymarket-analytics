import sys
from datetime import datetime, timezone
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from models.dataclasses import WalletProfile, Signal2Score


def test_wallet_profile_defaults():
    profile = WalletProfile(
        wallet_address="0xabc",
        first_trade_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        total_trades=10,
        unique_markets=3,
        longshot_attempts=5,
        longshot_wins=2,
        longshot_win_rate=0.4,
        avg_implied_prob_at_entry=0.15,
        top_categories=["fed", "crypto"],
        category_concentration=0.6,
        avg_days_before_resolution=2.5,
        new_account_flag=True,
        avg_bet_size=1500.0,
    )
    assert profile.insider_score == 0.0
    assert profile.score_components == {}


def test_signal2_score_fields():
    score = Signal2Score(
        market_id="0xdeadbeef",
        timestamp=1234.0,
        market_insider_risk=0.42,
        high_score_wallet_count=3,
        avg_insider_score=0.55,
        sample_size=12,
        confidence=0.1008,
    )
    assert score.market_id == "0xdeadbeef"
    assert score.sample_size == 12
