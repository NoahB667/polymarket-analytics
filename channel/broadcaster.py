"""Posts AnomalyEvents to the premium and free Telegram channels
(reference/signal_design.md "broadcaster.py Flow"). Premium posts
immediately; free is enqueued onto the AlertQueue with a delay so it
always lags premium by at least delay_seconds.
"""

import logging
import time
from typing import Any, Callable, Dict

from blockchain.log_sanitizer import redact_urls
from channel.alert_queue import AlertQueue
from channel.formatter import format_free_alert, format_premium_alert
from models.orm import AnomalyEvent

logger = logging.getLogger("polymarket.channel.broadcaster")


def dispatch(
    event: AnomalyEvent,
    send_fn: Callable[[str, str], None],
    alert_queue: AlertQueue,
    premium_channel_id: str,
    free_channel_id: str,
    delay_seconds: float,
    daily_volume: float = 0.0,
) -> None:
    """Posts event to premium immediately (if flagged) and enqueues the
    free-channel post with delay_seconds lag (if flagged).

    Mutates event.posted_at_premium in place on a successful premium post
    -- the caller (analytics/anomaly_engine.py) is responsible for
    persisting that back to the DB, matching the append-only AnomalyEvent
    row's "populated after posting" fields.

    The free-channel message is rendered here, eagerly, while `event` is
    still a live, attribute-readable object -- this function runs before
    the caller ever calls db.add(event)/db.commit() (see
    analytics/anomaly_engine.py's run_anomaly_engine_cycle). Only a plain
    dict payload (chat_id/message/slug) is put on the AlertQueue, never
    the ORM instance itself: by the time the AlertQueue worker thread
    fires (delay_seconds later, on its own thread), the session that
    produced `event` has long since committed and closed, and with this
    project's expire_on_commit=True SessionLocal default, any attribute
    access on a detached `event` at that point would raise
    DetachedInstanceError. There is therefore no way to set
    event.posted_at_free from the delayed path -- see the posted_at_free
    column comment in models/orm.py.
    """
    if event.broadcast_premium:
        try:
            message = format_premium_alert(event, daily_volume=daily_volume)
            send_fn(premium_channel_id, message)
            event.posted_at_premium = time.time()
        except Exception as e:
            logger.error(f"Broadcaster: premium post failed for {event.slug}: {redact_urls(e)}")

    if event.broadcast_free:
        payload = {
            "chat_id": free_channel_id,
            "message": format_free_alert(event),
            "slug": event.slug,
        }
        alert_queue.enqueue(payload, ready_at=time.time() + delay_seconds)


def dispatch_free(
    payload: Dict[str, str],
    send_fn: Callable[[str, str], None],
) -> None:
    """The AlertQueue worker's dispatch_fn -- posts the delayed free alert.

    Takes a plain dict payload (chat_id/message/slug) rather than the
    AnomalyEvent ORM object -- see dispatch()'s docstring for why: by the
    time this runs, the session that produced the original event has been
    closed and any attribute access on it would raise
    DetachedInstanceError. Because there is no ORM object here, this
    function cannot set posted_at_free (see models/orm.py's
    posted_at_free column comment for the resulting limitation).
    """
    try:
        send_fn(payload["chat_id"], payload["message"])
    except Exception as e:
        logger.error(f"Broadcaster: free post failed for {payload.get('slug')}: {redact_urls(e)}")
