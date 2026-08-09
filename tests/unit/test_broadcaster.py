import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from channel.alert_queue import AlertQueue
from channel.broadcaster import dispatch
from models.orm import AnomalyEvent


def _event(**overrides):
    defaults = dict(
        market_id="0xabc", slug="test-market", question="Will X happen?",
        category="politics", timestamp=time.time(), trigger="OFI_SPIKE",
        severity="MEDIUM", anomaly_score=0.65, current_price=0.42,
        price_change_pct=3.1, ofi_15min=0.61, volume_spike_ratio=1.2,
        is_long_shot=False, buy_pressure_pct=80.5, anomalous_wallet_count=0,
        market_insider_risk=0.0, wallet_context_available=False,
        broadcast_free=True, broadcast_premium=True, broadcast_reason="OFI_SPIKE detected",
    )
    defaults.update(overrides)
    return AnomalyEvent(**defaults)


def test_premium_posted_immediately_when_flagged():
    send_fn = MagicMock()
    queue = AlertQueue()
    event = _event(broadcast_premium=True, broadcast_free=False)

    dispatch(event, send_fn=send_fn, alert_queue=queue, premium_channel_id="PREM", free_channel_id="FREE", delay_seconds=60.0)

    send_fn.assert_called_once()
    chat_id, message = send_fn.call_args[0]
    assert chat_id == "PREM"
    assert "Will X happen?" in message
    assert event.posted_at_premium is not None


def test_free_enqueued_with_delay_not_posted_immediately():
    send_fn = MagicMock()
    queue = AlertQueue()
    event = _event(broadcast_premium=False, broadcast_free=True)

    before = time.time()
    dispatch(event, send_fn=send_fn, alert_queue=queue, premium_channel_id="PREM", free_channel_id="FREE", delay_seconds=60.0)

    send_fn.assert_not_called()
    assert len(queue._heap) == 1
    ready_at = queue._heap[0][0]
    assert ready_at >= before + 60.0


def test_neither_flag_set_does_nothing():
    send_fn = MagicMock()
    queue = AlertQueue()
    event = _event(broadcast_premium=False, broadcast_free=False)

    dispatch(event, send_fn=send_fn, alert_queue=queue, premium_channel_id="PREM", free_channel_id="FREE", delay_seconds=60.0)

    send_fn.assert_not_called()
    assert len(queue._heap) == 0
