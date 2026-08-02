import sys
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from models.dataclasses import Signal2Score


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
    assert score.timestamp == 1234.0
    assert score.market_insider_risk == 0.42
    assert score.high_score_wallet_count == 3
    assert score.avg_insider_score == 0.55
    assert score.sample_size == 12
    assert score.confidence == 0.1008
