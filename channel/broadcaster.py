"""Posts AnomalyEvents to the premium and free Telegram channels
(reference/signal_design.md "broadcaster.py Flow"). Premium posts
immediately; free is enqueued onto the AlertQueue with a delay so it
always lags premium by at least delay_seconds.
"""

import logging
import time
from typing import Any, Callable

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
    """
    if event.broadcast_premium:
        try:
            message = format_premium_alert(event, daily_volume=daily_volume)
            send_fn(premium_channel_id, message)
            event.posted_at_premium = time.time()
        except Exception as e:
            logger.error(f"Broadcaster: premium post failed for {event.slug}: {e}")

    if event.broadcast_free:
        alert_queue.enqueue(event, ready_at=time.time() + delay_seconds)


def dispatch_free(
    event: AnomalyEvent,
    send_fn: Callable[[str, str], None],
    free_channel_id: str,
) -> None:
    """The AlertQueue worker's dispatch_fn -- posts the delayed free alert."""
    try:
        message = format_free_alert(event)
        send_fn(free_channel_id, message)
        event.posted_at_free = time.time()
    except Exception as e:
        logger.error(f"Broadcaster: free post failed for {event.slug}: {e}")
