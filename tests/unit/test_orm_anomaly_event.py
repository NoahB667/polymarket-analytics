import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import AnomalyEvent, PriceImpactCheck


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine), engine


def test_anomaly_event_round_trips():
    Session, _ = _session_factory()
    db = Session()
    db.add(AnomalyEvent(
        market_id="0xabc", slug="test-market", question="Q?", category="politics",
        timestamp=time.time(), trigger="OFI_SPIKE", severity="MEDIUM", anomaly_score=0.65,
        current_price=0.42, price_change_pct=3.1, ofi_15min=0.61, volume_spike_ratio=1.2,
        is_long_shot=False, buy_pressure_pct=80.5, anomalous_wallet_count=0,
        market_insider_risk=0.0, wallet_context_available=False,
        broadcast_free=False, broadcast_premium=True, broadcast_reason="OFI_SPIKE detected",
    ))
    db.commit()
    row = db.query(AnomalyEvent).one()
    assert row.id is not None
    assert row.trigger == "OFI_SPIKE"
    assert row.posted_at_premium is None
    assert row.posted_at_free is None


def test_price_impact_check_has_anomaly_event_id_column():
    _, engine = _session_factory()
    columns = {c["name"] for c in inspect(engine).get_columns("price_impact_checks")}
    assert "anomaly_event_id" in columns
