import sys
from pathlib import Path
import pytest

# Ensure the built extension module is importable before loading core.cpp_bridge.
build_path = Path(__file__).resolve().parents[2] / "cpp" / "build"
sys.path.append(str(build_path))

from core.cpp_bridge import CoreEngineBridge  # noqa: E402

@pytest.mark.skipif(not CoreEngineBridge().is_cpp_available(), reason="C++ core not built")
def test_cpp_bridge_priority_flow():
    engine = CoreEngineBridge(pool_capacity=8, queue_capacity=8)

    engine.update_subscription(chat_id=1, market_hash=1, min_usd=0.0)
    payload = '{"event_type":"last_trade_price","market":"mkt","asset_id":"1","price":"0.05","size":"700000","side":"BUY","timestamp":"1"}'
    # 1. Process
    assert engine.process_message(payload) is True
    
    # 2. Inspect Stats
    stats = engine.get_stats()
    print(f"\nDEBUG STATS: {stats}")
    
    # 3. Check for specific failure modes in stats
    assert stats["processed_total"] == 1, "Engine reported success but didn't increment processed count"
    assert stats["push_fail_total"] == 0, f"Trade was dropped! Stats: {stats}"
    
    # 4. Check queues
    trade = engine.pop_priority() or engine.pop_normal()
    assert trade is not None, "Trade vanished after processing"


def test_cpp_bridge_passes_signal_core_thresholds(monkeypatch):
    """CoreEngineBridge must source both trade-filter thresholds from signal_core
    and forward them positionally to the C++ constructor -- this is the one
    place the real calibrated values cross from private signal_core into the
    running system."""
    import core.cpp_bridge as cpp_bridge_module

    captured_args = {}

    class _FakeCoreEngine:
        def __init__(self, pool_capacity, queue_capacity, long_shot_price_threshold, large_trade_usd_threshold):
            captured_args["pool_capacity"] = pool_capacity
            captured_args["queue_capacity"] = queue_capacity
            captured_args["long_shot_price_threshold"] = long_shot_price_threshold
            captured_args["large_trade_usd_threshold"] = large_trade_usd_threshold

    monkeypatch.setattr(cpp_bridge_module._cpp, "CoreEngine", _FakeCoreEngine)

    cpp_bridge_module.CoreEngineBridge(pool_capacity=16, queue_capacity=16)

    assert captured_args["pool_capacity"] == 16
    assert captured_args["queue_capacity"] == 16
    assert captured_args["long_shot_price_threshold"] == cpp_bridge_module.LONG_SHOT_PRICE_THRESHOLD
    assert captured_args["large_trade_usd_threshold"] == cpp_bridge_module.LARGE_TRADE_USD_THRESHOLD
