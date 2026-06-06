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
