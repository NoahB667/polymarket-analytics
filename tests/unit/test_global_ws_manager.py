import sys
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from core.global_ws_manager import GlobalWebSocketManager


def test_add_market_registers_asset_ids_and_callback():
    manager = GlobalWebSocketManager(url="wss://example.test")
    received = []

    manager.add_market("fed-rate-june", ["tok_1", "tok_2"], received.append)

    assert set(manager.current_asset_ids()) == {"tok_1", "tok_2"}
    with manager._lock:
        assert manager._routing_table["tok_1"] == "fed-rate-june"
        assert manager._callbacks["fed-rate-june"] is received.append


def test_remove_market_clears_routing_table_and_callback():
    manager = GlobalWebSocketManager(url="wss://example.test")
    manager.add_market("fed-rate-june", ["tok_1", "tok_2"], lambda d: None)

    manager.remove_market("fed-rate-june")

    assert manager.current_asset_ids() == []
    with manager._lock:
        assert "fed-rate-june" not in manager._callbacks
