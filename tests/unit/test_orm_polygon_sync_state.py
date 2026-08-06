import sys
import time
from pathlib import Path

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models.orm import PolygonSyncState, WalletProfile


def _sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_polygon_sync_state_roundtrip():
    db = _sqlite_session()
    db.add(PolygonSyncState(last_block=100, last_updated=time.time(), events_processed=5))
    db.commit()
    row = db.query(PolygonSyncState).first()
    assert row.last_block == 100
    assert row.events_processed == 5


def test_wallet_profile_score_stale_defaults_false():
    db = _sqlite_session()
    db.add(WalletProfile(wallet_address="0xabc", last_updated=time.time()))
    db.commit()
    row = db.query(WalletProfile).first()
    assert row.score_stale is False
