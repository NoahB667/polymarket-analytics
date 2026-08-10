import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from channel.alert_queue import AlertQueue
from channel.broadcaster import dispatch, dispatch_free
from db import Base
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


def test_free_dispatch_enqueues_plain_payload_not_orm_object():
    """dispatch() must never put the live ORM instance on the AlertQueue --
    see channel/broadcaster.py's dispatch() docstring. Asserting the queued
    item is a plain dict (not an AnomalyEvent) guards against regressing
    back to the DetachedInstanceError bug directly, without needing a full
    session lifecycle for this particular assertion.
    """
    send_fn = MagicMock()
    queue = AlertQueue()
    event = _event(broadcast_premium=False, broadcast_free=True, slug="payload-check")

    dispatch(event, send_fn=send_fn, alert_queue=queue, premium_channel_id="PREM", free_channel_id="FREE", delay_seconds=60.0)

    assert len(queue._heap) == 1
    payload = queue._heap[0][2]
    assert isinstance(payload, dict)
    assert not isinstance(payload, AnomalyEvent)
    assert payload["chat_id"] == "FREE"
    assert payload["slug"] == "payload-check"
    assert "Will X happen?" in payload["message"]


def test_dispatch_free_sends_payload_message_to_chat_id():
    send_fn = MagicMock()
    payload = {"chat_id": "FREE", "message": "Unusual activity detected", "slug": "test-market"}

    dispatch_free(payload, send_fn=send_fn)

    send_fn.assert_called_once_with("FREE", "Unusual activity detected")


def test_dispatch_free_logs_without_raising_on_send_failure():
    def _boom(chat_id, message):
        raise RuntimeError("network error")

    payload = {"chat_id": "FREE", "message": "hi", "slug": "test-market"}

    # Must not raise -- dispatch_free wraps send_fn in its own try/except.
    dispatch_free(payload, send_fn=_boom)


def test_free_channel_post_survives_a_real_closed_db_session():
    """Regression test for the whole-branch-review Finding 1 bug: the free
    channel never posted anything in production because dispatch()used to
    enqueue the live SQLAlchemy AnomalyEvent instance itself, and by the
    time the AlertQueue worker thread fired 60s+ later, the session that
    produced it had already committed (which, with this project's default
    expire_on_commit=True SessionLocal, expires every attribute) and
    closed (detaching the instance) -- exactly like
    analytics/anomaly_engine.py's run_anomaly_engine_cycle does in
    production (db.add(event); db.commit(); ... db.close() in `finally`).
    Any attribute access on `event` after that raised DetachedInstanceError
    for every single free-channel post.

    This test builds a real AnomalyEvent, adds it to a real SQLAlchemy
    session, commits, and closes the session -- then exercises the full
    dispatch() + AlertQueue worker-thread free-channel path and asserts it
    succeeds with the correct message content sent, instead of silently
    failing.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    event = _event(broadcast_premium=False, broadcast_free=True, slug="closed-session-market")

    send_fn = MagicMock()
    queue = AlertQueue()

    # dispatch() must run BEFORE db.add()/commit()/close(), matching
    # run_anomaly_engine_cycle's ordering (broadcast_fn runs, then the
    # event is persisted) -- this is what makes the eager free-message
    # rendering in dispatch() safe.
    dispatch(
        event, send_fn=send_fn, alert_queue=queue,
        premium_channel_id="PREM", free_channel_id="FREE", delay_seconds=0.05,
    )

    try:
        db.add(event)
        db.commit()  # expire_on_commit=True (SQLAlchemy default) expires
        # every attribute on `event`, so any access below would trigger a
        # fresh SELECT -- which then fails once the session is closed.
    finally:
        db.close()  # detaches `event` entirely; any attribute access now
        # raises DetachedInstanceError.

    # Now fire the AlertQueue worker thread exactly as app.py wires it,
    # long after the session above is closed.
    stop_event = threading.Event()
    worker = threading.Thread(
        target=queue.run_worker,
        kwargs={
            "dispatch_fn": lambda payload: dispatch_free(payload, send_fn=send_fn),
            "stop_event": stop_event,
            "poll_interval": 0.02,
        },
        daemon=True,
    )
    worker.start()
    time.sleep(0.3)
    stop_event.set()
    worker.join(timeout=1.0)

    send_fn.assert_called_once()
    chat_id, message = send_fn.call_args[0]
    assert chat_id == "FREE"
    assert "Will X happen?" in message
    assert "Unusual activity detected" in message
