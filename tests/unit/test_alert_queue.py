import sys
import threading
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from channel.alert_queue import AlertQueue


def test_dispatch_waits_until_ready_at():
    queue = AlertQueue()
    dispatched = []
    queue.enqueue("event-a", ready_at=time.time() + 0.2)

    stop_event = threading.Event()
    worker = threading.Thread(
        target=queue.run_worker,
        kwargs={"dispatch_fn": dispatched.append, "stop_event": stop_event, "poll_interval": 0.05},
        daemon=True,
    )
    worker.start()

    time.sleep(0.05)
    assert dispatched == []  # not ready yet

    time.sleep(0.3)
    assert dispatched == ["event-a"]

    stop_event.set()
    worker.join(timeout=1.0)


def test_dispatch_failure_does_not_stop_the_worker():
    queue = AlertQueue()
    calls = []

    def flaky_dispatch(event):
        calls.append(event)
        if event == "bad":
            raise RuntimeError("boom")

    queue.enqueue("bad", ready_at=time.time())
    queue.enqueue("good", ready_at=time.time())

    stop_event = threading.Event()
    worker = threading.Thread(
        target=queue.run_worker,
        kwargs={"dispatch_fn": flaky_dispatch, "stop_event": stop_event, "poll_interval": 0.05},
        daemon=True,
    )
    worker.start()
    time.sleep(0.3)
    stop_event.set()
    worker.join(timeout=1.0)

    assert set(calls) == {"bad", "good"}
