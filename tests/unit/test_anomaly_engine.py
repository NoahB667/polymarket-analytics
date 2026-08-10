import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import orjson
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.dataclasses import Signal2Score
from models.orm import AnomalyEvent, AutoSubscription, PriceImpactCheck

import analytics.anomaly_engine as ae


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _fake_redis(signal1_by_slug):
    r = MagicMock()

    def get(key):
        if key.startswith("signal:1:score:"):
            slug = key.split(":", 3)[-1]
            payload = signal1_by_slug.get(slug)
            return orjson.dumps(payload) if payload else None
        return None

    r.get.side_effect = get
    r.setex.side_effect = lambda *a, **k: None
    return r


def _auto_subscription(**overrides):
    defaults = dict(
        slug="test-market", question="Will X happen?", category="politics",
        condition_id=None, market_score=0.9, tier=1, volume_24h=100_000.0,
        subscribed_at=time.time(), status="active", token_ids=["tok-yes", "tok-no"],
    )
    defaults.update(overrides)
    return AutoSubscription(**defaults)


def _strong_signal1(direction="BUY"):
    return {
        "score": 0.9 if direction == "BUY" else -0.9,
        "confidence": 0.9,
        "direction": direction,
        "volume_spike_ratio": 6.0,
        "long_shot_triggered": True,
        "slug": "test-market",
        "latest_price": 0.2,
        "updated_at": time.time(),
        "metrics": {"ofi_1m": 0.9, "ofi_5m": 0.9, "ofi_15m": 0.9, "ofi_1h": 0.9},
    }


def test_no_signal1_cached_skips_market():
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription())
    db.commit()
    redis_client = _fake_redis({})
    broadcast_fn = MagicMock()

    summary = ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    assert summary == {"evaluated": 0, "generated": 0}
    broadcast_fn.assert_not_called()


def test_strong_signal_generates_and_persists_anomaly_event():
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription())
    db.commit()
    redis_client = _fake_redis({"test-market": _strong_signal1()})
    broadcast_fn = MagicMock()

    summary = ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    assert summary["generated"] == 1
    broadcast_fn.assert_called_once()
    db2 = Session()
    rows = db2.query(AnomalyEvent).all()
    assert len(rows) == 1
    assert rows[0].slug == "test-market"


def test_cooldown_suppresses_repeat_non_critical_alert():
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription(condition_id="0xabc"))
    db.commit()
    redis_client = _fake_redis({"test-market": _strong_signal1()})
    redis_client.get.side_effect = lambda key: (
        orjson.dumps(_strong_signal1()) if key == "signal:1:score:test-market"
        else (str(time.time()).encode() if key == "alert:last:0xabc" else None)
    )
    broadcast_fn = MagicMock()

    with patch(
        "analytics.anomaly_engine.build_signal2_score",
        return_value=Signal2Score(
            market_id="0xabc", timestamp=time.time(), market_insider_risk=0.9,
            high_score_wallet_count=3, avg_insider_score=0.9, sample_size=10, confidence=0.9,
        ),
    ):
        summary = ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    # With condition_id set and Signal 2 mocked to fire wallet_cluster too,
    # all four trigger conditions fire -> CRITICAL severity -> bypasses cooldown.
    assert summary["generated"] == 1


def test_broadcast_mutation_persists_without_update_after_insert():
    """Regression test: broadcast_fn mutates event.posted_at_premium in place
    (as broadcaster.dispatch() does in production). run_anomaly_engine_cycle
    must flush (not commit) before broadcast_fn runs and commit exactly once
    afterward, so the row lands with its final state via a single INSERT --
    never an UPDATE, which would violate anomaly_event's append-only
    invariant (models/orm.py).
    """
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription())
    db.commit()
    redis_client = _fake_redis({"test-market": _strong_signal1()})

    mutated_ts = time.time()

    def broadcast_fn(db_arg, event):
        event.posted_at_premium = mutated_ts

    executed_statements = []
    from sqlalchemy import event as sa_event

    engine = Session.kw["bind"]

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement)

    summary = ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    assert summary["generated"] == 1
    assert not any(s.strip().upper().startswith("UPDATE") for s in executed_statements)

    db2 = Session()
    rows = db2.query(AnomalyEvent).all()
    assert len(rows) == 1
    assert rows[0].posted_at_premium == mutated_ts


def test_price_impact_check_failure_does_not_lose_anomaly_event():
    """Regression test: if inserting PriceImpactCheck rows fails partway
    through _schedule_price_impact_checks, that function's internal
    db.rollback() must not undo the AnomalyEvent, which is committed on its
    own (db.add(event); db.commit()) before price-impact scheduling ever
    touches the session. Simulated here by making the PriceImpactCheck
    constructor raise on its second call, which is caught and rolled back
    inside _schedule_price_impact_checks -- the AnomalyEvent must still be
    present in the DB afterward.
    """
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription())
    db.commit()
    redis_client = _fake_redis({"test-market": _strong_signal1()})
    broadcast_fn = MagicMock()

    call_count = {"n": 0}
    real_price_impact_check = ae.PriceImpactCheck

    def flaky_price_impact_check(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated insert failure")
        return real_price_impact_check(*args, **kwargs)

    with patch("analytics.anomaly_engine.PriceImpactCheck", side_effect=flaky_price_impact_check):
        summary = ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    assert summary["generated"] == 1

    db2 = Session()
    events = db2.query(AnomalyEvent).filter_by(slug="test-market").all()
    assert len(events) == 1, "AnomalyEvent must survive a PriceImpactCheck insert failure"

    # The price-impact-check scheduling itself was rolled back best-effort,
    # so no partial PriceImpactCheck rows should have landed either.
    checks = db2.query(PriceImpactCheck).all()
    assert len(checks) == 0


def test_high_or_critical_schedules_price_impact_checks():
    Session = _session_factory()
    db = Session()
    db.add(_auto_subscription())
    db.commit()
    redis_client = _fake_redis({"test-market": _strong_signal1()})
    broadcast_fn = MagicMock()

    ae.run_anomaly_engine_cycle(Session, redis_client, broadcast_fn)

    db2 = Session()
    checks = db2.query(PriceImpactCheck).all()
    assert len(checks) == 5  # 5m, 15m, 1h, 4h, 24h
    assert {c.checkpoint_interval for c in checks} == {"5m", "15m", "1h", "4h", "24h"}
    assert all(c.anomaly_event_id is not None for c in checks)
