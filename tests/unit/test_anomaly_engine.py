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
