"""Data transfer objects for signal generation.

Kept separate from models/orm.py: these are pure in-memory DTOs consumed by
analytics/wallet_intelligence.py and blockchain/wallet_profiler.py, distinct
from the persistence-layer WalletProfile ORM model in models/orm.py.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class WalletProfile:
    """Aggregated on-chain behavioral profile for a single wallet address."""

    wallet_address: str
    first_trade_date: datetime
    total_trades: int
    unique_markets: int

    # Long-shot performance
    longshot_attempts: int          # trades where entry price < 0.20
    longshot_wins: int              # correctly predicted long-shot outcomes
    longshot_win_rate: float        # actual wins / attempts
    avg_implied_prob_at_entry: float  # expected win rate (baseline)

    # Market concentration
    top_categories: List[str]       # most traded categories, most-common first
    category_concentration: float   # 1.0 = only one category

    # Timing
    avg_days_before_resolution: float
    new_account_flag: bool          # first_trade_date < 30 days ago
    avg_bet_size: float

    # Score (populated by analytics.wallet_intelligence.calculate_insider_score)
    insider_score: float = 0.0
    score_components: Dict[str, float] = field(default_factory=dict)


@dataclass
class Signal2Score:
    """Per-market aggregation of wallet intelligence."""

    market_id: str
    timestamp: float
    market_insider_risk: float    # 0.0 to 1.0 (fraction suspicious volume)
    high_score_wallet_count: int  # count of wallets with score > 0.6
    avg_insider_score: float      # average score of active wallets
    sample_size: int              # number of wallets analyzed
    confidence: float             # based on sample size
