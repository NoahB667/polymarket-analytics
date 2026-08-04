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
