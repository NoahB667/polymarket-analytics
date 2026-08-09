import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from models.orm import AnomalyEvent
from channel.formatter import contains_forbidden_language, format_free_alert, format_premium_alert


def _event(**overrides):
    defaults = dict(
        market_id="0xabc", slug="test-market", question="Will X happen?",
        category="politics", timestamp=time.time(), trigger="OFI_SPIKE",
        severity="MEDIUM", anomaly_score=0.65, current_price=0.42,
        price_change_pct=3.1, ofi_15min=0.61, volume_spike_ratio=1.2,
        is_long_shot=False, buy_pressure_pct=80.5, anomalous_wallet_count=0,
        market_insider_risk=0.0, wallet_context_available=False,
        broadcast_free=False, broadcast_premium=True, broadcast_reason="OFI_SPIKE detected",
    )
    defaults.update(overrides)
    return AnomalyEvent(**defaults)


def test_premium_alert_contains_core_fields():
    text = format_premium_alert(_event(), daily_volume=125_000.0)
    assert "Will X happen?" in text
    assert "42% implied" in text
    assert "Market surveillance only. Not financial advice." in text
    assert "#politics" in text


def test_premium_alert_omits_wallet_line_when_unavailable():
    text = format_premium_alert(_event(wallet_context_available=False))
    assert "anomalous" not in text.lower()


def test_premium_alert_includes_wallet_line_when_available():
    text = format_premium_alert(_event(
        wallet_context_available=True, anomalous_wallet_count=3, market_insider_risk=0.4,
    ))
    assert "3 wallets" in text


def test_premium_alert_includes_longshot_line_when_flagged():
    text = format_premium_alert(_event(is_long_shot=True, current_price=0.18))
    assert "Long-shot" in text


def test_free_alert_has_no_ofi_numbers_or_wallet_context():
    text = format_free_alert(_event(anomalous_wallet_count=5, market_insider_risk=0.9))
    assert "ofi" not in text.lower()
    assert "wallet" not in text.lower()
    assert "Market surveillance only. Not financial advice." in text


def test_no_forbidden_language_in_either_template():
    premium = format_premium_alert(_event(
        wallet_context_available=True, anomalous_wallet_count=2, is_long_shot=True, current_price=0.2,
    ))
    free = format_free_alert(_event())
    assert contains_forbidden_language(premium) == []
    assert contains_forbidden_language(free) == []


def test_contains_forbidden_language_detects_violations():
    assert "buy" in contains_forbidden_language("You should buy this now")
    assert "should" in contains_forbidden_language("You should buy this now")
