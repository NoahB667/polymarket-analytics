"""Delayed dispatch queue for the free channel's 60s lag behind premium
(reference/mvp_product.md: "the 60-second delay is the monetization
mechanic"). Thread-based (matches this project's existing background-loop
convention -- see analytics/signal_combiner.py, core/wallet_intelligence_scheduler.py
-- rather than asyncio, since it must run on its own daemon thread started
from app.py's lifespan, not inside an event loop).
"""

import heapq
import logging
import threading
import time
from typing import Any, Callable, Optional

from blockchain.log_sanitizer import redact_urls

logger = logging.getLogger("polymarket.channel.alert_queue")


class AlertQueue:
    """A min-heap of (ready_at, sequence, payload) tuples, thread-safe.

    Generic over payload type -- `payload` can be any object accepted by
    the dispatch_fn passed to run_worker(). channel/broadcaster.py's
    dispatch() enqueues a plain dict (chat_id/message/slug), not an ORM
    object, so this class holds no assumptions about SQLAlchemy sessions.
    """

    def __init__(self) -> None:
        self._heap: list = []
        self._lock = threading.Lock()
        self._counter = 0

    def enqueue(self, event: Any, ready_at: float) -> None:
        """Schedules `event` (any payload) for dispatch once ready_at passes.

        Args:
            event: The payload to hand to run_worker()'s dispatch_fn once
                ready. Not necessarily an ORM object -- see class docstring.
            ready_at: Unix timestamp (time.time()) after which this payload
                becomes eligible for dispatch.
        """
        with self._lock:
            heapq.heappush(self._heap, (ready_at, self._counter, event))
            self._counter += 1

    def _pop_ready(self, now: float) -> list:
        ready = []
        with self._lock:
            while self._heap and self._heap[0][0] <= now:
                _, _, event = heapq.heappop(self._heap)
                ready.append(event)
        return ready

    def run_worker(
        self,
        dispatch_fn: Callable[[Any], None],
        stop_event: Optional[threading.Event] = None,
        poll_interval: float = 1.0,
    ) -> None:
        """Polls for ready events and dispatches them, forever.

        Never raises -- a failed dispatch is logged and the worker keeps
        polling (CLAUDE.md rule 3: best-effort, never stop the pipeline).
        """
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            for event in self._pop_ready(time.time()):
                try:
                    dispatch_fn(event)
                except Exception as e:
                    logger.error(f"Alert queue: dispatch failed, continuing: {redact_urls(e)}")
            stop_event.wait(poll_interval)
