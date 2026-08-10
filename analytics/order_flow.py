import time
import threading
import collections
from typing import *

import requests
from sqlalchemy import func

from db import get_db_session
from models.orm import PriceImpactCheck, Trade
try:
    from signal_core.order_flow import generate_signal_score as _score_order_flow
except ImportError as e:
    raise ImportError(
        "signal_core package missing. Run: git submodule update --init && pip install -e vendor/signal-core"
    ) from e

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

def calculate_volume_usd(slug: str, window_minutes: int) -> float:
    """Total USD volume (both sides) traded on `slug` in the trailing
    window_minutes -- lets signal_core gate OFI readings on real dollar
    volume instead of firing on a single trade in a thin market.
    """
    with lock:
        if slug not in market_windows:
            return 0.0
        trades = market_windows[slug]
        start_time = time.time() - (window_minutes * 60)
        total = 0.0
        for trade in reversed(trades):
            if trade["timestamp"] < start_time:
                break
            total += float(trade.get("usd", 0.0))
        return total


def calculate_volume_1h_usd(slug: str) -> float:
    """Total USD volume (both sides) traded on `slug` in the trailing hour.
    append_trade already evicts anything older than 60 minutes, so summing
    the whole in-memory window is exactly the trailing-1h USD total.
    """
    with lock:
        if slug not in market_windows:
            return 0.0
        return sum(float(trade.get("usd", 0.0)) for trade in market_windows[slug])


def read_volume_24h_baseline(slug: str, redis_client) -> float:
    """Average hourly USD volume for `slug`, cached in Redis by the anomaly
    engine's periodic baseline refresh (analytics/anomaly_engine.py). 0.0 if
    unknown -- callers must treat that as "no baseline available", not
    "the market has zero typical volume".
    """
    try:
        baseline_bytes = redis_client.get(f"meta:volume_24h_avg:{slug}")
        if baseline_bytes:
            return float(baseline_bytes)
    except Exception:
        pass
    return 0.0


def calculate_volume_spike(slug: str, redis_client) -> float:
    with lock:
        if slug not in market_windows or not market_windows[slug]:
            return 1.0 # Return neutral ratio if no trades exist
    # Network I/O to Redis happens OUTSIDE the thread lock.
    # USD, not raw trade size -- shares/contracts aren't comparable to a
    # dollar-denominated baseline, and the mismatch is worst on extreme-
    # priced markets where 1 share is nowhere near $1.
    current_1h_volume = calculate_volume_1h_usd(slug)
    baseline_24h = read_volume_24h_baseline(slug, redis_client)
    if baseline_24h > 0:
        return current_1h_volume / baseline_24h
    return 1.0

def calculate_price_change_pct(slug: str, window_minutes: int = 20) -> float:
    """Percent price change over the trailing window_minutes, from the
    per-slug in-memory trade deque (reference/PROJECT_CONTEXT.md AnomalyEvent
    "price_change_pct" field: "price change in last 20 minutes").
    """
    with lock:
        if slug not in market_windows or not market_windows[slug]:
            return 0.0
        trades = market_windows[slug]
        latest_price = trades[-1]["price"]
        start_time = time.time() - (window_minutes * 60)
        window_open_price = latest_price
        for trade in trades:
            if trade["timestamp"] >= start_time:
                window_open_price = trade["price"]
                break
        if window_open_price == 0.0:
            return 0.0
        return round((latest_price - window_open_price) / window_open_price * 100.0, 2)

def calculate_daily_volume(db, slug: str) -> float:
    """Total USD volume traded on `slug` since the start of the current UTC
    day (reference/PROJECT_CONTEXT.md AnomalyEvent alert "Total market
    volume today" field).
    """
    day_start = (int(time.time() // 86400)) * 86400
    total = db.query(func.sum(Trade.usd)).filter(
        Trade.slug == slug, Trade.timestamp >= day_start
    ).scalar()
    return float(total or 0.0)


def calculate_volume_24h_baseline(db, slug: str) -> float:
    """Average hourly USD volume for `slug` over the trailing 24 hours --
    the baseline calculate_volume_spike() compares current_1h_volume
    against (reference/signal_design.md "Volume Spike Ratio").
    """
    window_start = time.time() - 86400
    total = db.query(func.sum(Trade.usd)).filter(
        Trade.slug == slug, Trade.timestamp >= window_start
    ).scalar()
    return float(total or 0.0) / 24.0


def generate_signal_score(slug: str, latest_price: float, redis_client) -> dict:
    ofi_1m = calculate_ofi(slug, window_minutes=1)
    ofi_5m = calculate_ofi(slug, window_minutes=5)
    ofi_15m = calculate_ofi(slug, window_minutes=15)
    ofi_1h = calculate_ofi(slug, window_minutes=60)

    volume_spike = calculate_volume_spike(slug, redis_client)

    result = _score_order_flow(ofi_1m, ofi_5m, ofi_15m, ofi_1h, volume_spike, latest_price)
    result["slug"] = slug
    result["latest_price"] = latest_price
    result["updated_at"] = time.time()
    result["volume_15m_usd"] = calculate_volume_usd(slug, window_minutes=15)
    result["volume_1h_usd"] = calculate_volume_1h_usd(slug)
    result["baseline_hourly_usd"] = read_volume_24h_baseline(slug, redis_client)
    return result

def price_impact_evaluator_worker():
    """
    Background worker loop that scans the database for past-due price impact
    tracking records and updates them against the live Polymarket CLOB endpoints.
    """
    print("Price Impact Evaluator Worker started successfully.", flush=True)
    
    while True:
        try:
            current_time = time.time()
            
            with get_db_session() as db:
                # Query all active items whose target evaluation timestamps have been passed
                pending_checks = db.query(PriceImpactCheck).filter(
                    PriceImpactCheck.is_completed == False,
                    PriceImpactCheck.target_check_time <= current_time
                ).all()
                
                if pending_checks:
                    print(f"Found {len(pending_checks)} pending price impact checks to evaluate.", flush=True)
                    
                    for check in pending_checks:
                        # Query the live price midpoint from the Polymarket CLOB API using the asset token id
                        url = f"https://clob.polymarket.com/midpoint?token_id={check.asset_id}"
                        
                        try:
                            response = requests.get(url, timeout=5)
                            
                            if response.status_code == 200:
                                data = response.json()
                                current_price_str = data.get("mid_price") or data.get("mid")

                                if not current_price_str and isinstance(data, list) and len(data) > 0:
                                    current_price_str = data[0].get("mid_price") or data[0].get("mid")  
                                
                                if current_price_str:
                                    current_price = float(current_price_str)
                                    entry_price = float(check.entry_price)
                                    
                                    # Calculate mathematical directional delta
                                    price_diff = current_price - entry_price
                                    pct_change = (price_diff / entry_price) * 100.0
                                    
                                    # Invert percentage calculation if the tracker logged a short position
                                    if check.direction == "SELL":
                                        pct_change = -pct_change
                                        
                                    check.check_price = current_price
                                    check.price_change_pct = round(pct_change, 2)
                                    check.is_completed = True
                                    print(f"Evaluated ID {check.id} ({check.direction}): {pct_change:+.2f}%", flush=True)
                                else:
                                    print(f"Missing midpoint field structure for Asset ID: {check.asset_id}", flush=True)
                                    
                            elif response.status_code == 404:
                                # Mark completed to evict the dead asset token ID from polling queues gracefully
                                check.is_completed = True
                                print(f"Asset ID {check.asset_id} returned 404. Marked completed to clear queue.", flush=True)
                                
                            else:
                                print(f"API Error {response.status_code} evaluating Asset ID: {check.asset_id}", flush=True)
                                
                        except requests.RequestException as network_error:
                            print(f"Network connection failure checking ID {check.id}: {network_error}", flush=True)
                    
                    # Flush the entire batch changes to database storage
                    db.commit()
                    
        except Exception as general_error:
            print(f"Critical exception inside worker tracking execution: {general_error}", flush=True)
            
        # Hold execution state before looking for the next past-due cycle
        time.sleep(30)