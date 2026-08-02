import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import AutoSubscription


def test_auto_subscription_round_trips_token_ids_and_defaults():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(AutoSubscription(
        slug="fed-rate-june",
        question="Will the Fed cut rates in June?",
        category="economics",
        market_score=0.8,
        tier=1,
        volume_24h=85000.0,
        days_remaining=45.0,
        token_ids=["tok_1", "tok_2"],
        subscribed_at=time.time(),
        status="active",
    ))
    db.commit()

    fetched = db.query(AutoSubscription).filter_by(slug="fed-rate-june").first()
    assert fetched.token_ids == ["tok_1", "tok_2"]
    assert fetched.tier == 1
    assert fetched.status == "active"
    assert fetched.consecutive_misses == 0
    assert fetched.total_trades_collected == 0

    db.close()
