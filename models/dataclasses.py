"""Data transfer objects for signal generation.

Kept separate from models/orm.py: Signal2Score here is a pure in-memory DTO
consumed by blockchain/wallet_profiler.py. WalletProfile, the other DTO in
this signal-generation pipeline, now lives in signal_core.models.
"""

from dataclasses import dataclass


@dataclass
class Signal2Score:
    """Per-market aggregation of wallet intelligence."""

    market_id: str
    timestamp: float
    market_insider_risk: float  # 0.0 to 1.0 (fraction suspicious volume)
    high_score_wallet_count: int  # count of wallets with score > 0.6
    avg_insider_score: float  # average score of active wallets
    sample_size: int  # number of wallets analyzed
    confidence: float  # based on sample size


@dataclass
class CombinedSignal:
    """Signal 1 + Signal 2 combined into a single trade decision (Step 10).

    Direction comes from Signal 1 alone -- Signal 2 (wallet intelligence)
    has no directional concept, only an insider-risk magnitude. See
    docs/superpowers/plans/2026-08-07-signal-combiner-paper-trader.md.
    """

    market_id: str
    slug: str
    direction: str  # "BUY" or "SELL", from Signal 1
    combined_score: float  # 0.0 to 1.0
    signal1_confidence: float
    signal2_confidence: float
    signal2_market_insider_risk: float
    recommended_action: str  # "TRADE", "WATCH", or "IGNORE"
    gates_passed: bool
    timestamp: float
