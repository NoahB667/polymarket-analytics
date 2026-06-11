import time
import threading
import collections
from typing import *

market_windows = {}
lock = threading.Lock()

def append_trade(slug: str, trade: dict):
    with lock:
        if slug not in market_windows:
            market_windows[slug] = collections.deque()
        market_windows[slug].append(trade)
        # 60-minute trailing expiration boundary
        cutoff_time = time.time() - 3600
        while market_windows[slug] and market_windows[slug][0]["timestamp"] < cutoff_time:
            market_windows[slug].popleft() # Evict the stale trade from memory

def calculate_ofi(slug: str, window_minutes: int) -> float:
    with lock:
        if slug not in market_windows:
            return 0.0
        trades = market_windows[slug]
        buy_volume = 0.0
        sell_volume = 0.0
        start_time = time.time() - (window_minutes * 60)
        for trade in reversed(trades):
            if trade["timestamp"] < start_time:
                break
            # Accumulating the absolute trade sizes
            if trade.get("side") == "BUY":
                buy_volume += float(trade.get("size", 0.0))
            elif trade.get("side") == "SELL":
                sell_volume += float(trade.get("size", 0.0))
                
        total_volume = buy_volume + sell_volume
        # Guard clause: If no trades happened in this window, pressure is neutral
        if total_volume == 0.0:
            return 0.0
        return (buy_volume - sell_volume) / total_volume

def calculate_volume_spike(slug: str, redis_client) -> float:
    with lock:
        if slug not in market_windows or not market_windows[slug]:
            return 1.0 # Return neutral ratio if no trades exist
        # 1. Sum up every trade size in our 60-minute memory buffer
        current_1h_volume = sum(float(trade.get("size", 0.0)) for trade in market_windows[slug])
    # 2. Network I/O to Redis happens OUTSIDE the thread lock
    try:
        baseline_bytes = redis_client.get(f"meta:volume_24h_avg:{slug}")
        if baseline_bytes:
            baseline_24h = float(baseline_bytes)
            if baseline_24h > 0:
                return current_1h_volume / baseline_24h
    except Exception as e:
        pass
    return 1.0

def generate_signal_score(slug: str, latest_price: float, redis_client) -> dict:
    # 1. Extract our directional indicators across all horizons
    ofi_1m  = calculate_ofi(slug, window_minutes=1)
    ofi_5m  = calculate_ofi(slug, window_minutes=5)
    ofi_15m = calculate_ofi(slug, window_minutes=15)
    ofi_1h  = calculate_ofi(slug, window_minutes=60)
    
    # 2. Extract our market-relative volume metric
    volume_spike = calculate_volume_spike(slug, redis_client)
    
    # 3. Apply a weighted aggregation equation to compute the base score
    # Weights: 1m (40%), 5m (30%), 15m (20%), 1h (10%)
    base_ofi_score = (ofi_1m * 0.40) + (ofi_5m * 0.30) + (ofi_15m * 0.20) + (ofi_1h * 0.10)
    
    # 4. Check our Long-Shot asymmetric upside condition (Odds < 20%)
    is_long_shot = (latest_price < 0.20)
    
    # If it's a long shot and volume is surging, we amplify the signal weight!
    if is_long_shot and volume_spike > 1.5:
        confidence_multiplier = 1.5
    else:
        confidence_multiplier = 1.0
        
    # Compute the final bounded score
    final_score = base_ofi_score * confidence_multiplier
    final_score = max(-1.0, min(1.0, final_score))  # Force boundary cap between -1 and 1
    
    # 5. Determine trade directionality based on net order pressure
    direction = "BUY" if final_score >= 0 else "SELL"
    
    # Pack everything up into a clean data frame schema
    return {
        "slug": slug,
        "score": round(final_score, 4),
        "direction": direction,
        "volume_spike_ratio": round(volume_spike, 2),
        "long_shot_triggered": is_long_shot,
        "metrics": {
            "ofi_1m": round(ofi_1m, 2),
            "ofi_5m": round(ofi_5m, 2),
            "ofi_15m": round(ofi_15m, 2),
            "ofi_1h": round(ofi_1h, 2)
        },
        "updated_at": time.time()
    }