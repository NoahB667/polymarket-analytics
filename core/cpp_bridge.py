"""C++ bridge for high-performance trade processing with full pure-Python fallbacks."""

import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("polymarket.core.cpp_bridge")

try:
    import polymarket_core as _cpp
    _HAS_CPP = True
except Exception as e:
    _cpp = None
    _HAS_CPP = False
    logger.warning(f"polymarket_core C++ extension binary not found or failed to load ({e}). Using pure-Python fallback pipeline.")


@dataclass
class PythonStatsMock:
    """Mock metrics counter tracking state for the pure-Python fallback engine path."""
    received_total: int = 0
    processed_total: int = 0
    filter_matches: int = 0
    parse_errors: int = 0
    push_fail_total: int = 0
    pop_empty_total: int = 0
    normal_depth: int = 0
    priority_depth: int = 0
    pool_available: int = 1024
    latency_bucket_0_1us: int = 0
    latency_bucket_1_10us: int = 0
    latency_bucket_10_100us: int = 0
    latency_bucket_100_1000us: int = 0
    latency_bucket_1000us_plus: int = 0


class CoreEngineBridge:
    """Thin wrapper around the C++ CoreEngine with safe, allocation-minimal fallback arrays."""

    def __init__(self, pool_capacity: int = 1024, queue_capacity: int = 1024) -> None:
        self._engine = None
        self._pool_capacity = pool_capacity
        self._queue_capacity = queue_capacity
        
        if _HAS_CPP:
            try:
                self._engine = _cpp.CoreEngine(pool_capacity, queue_capacity)
            except Exception as e:
                logger.error(f"Failed to instantiate C++ CoreEngine ({e}). Falling back to Python runtime loops.")
                self._engine = None

        # Initialize native Python fallback objects if C++ is unavailable
        if self._engine is None:
            self._normal_queue: deque = deque(maxlen=queue_capacity)
            self._priority_queue: deque = deque(maxlen=queue_capacity)
            self._stats = PythonStatsMock(pool_available=pool_capacity)
            self._market_cache: Dict[str, int] = {}

    def is_cpp_available(self) -> bool:
        """Returns True if the high-performance C++ shared object engine is actively bound."""
        return self._engine is not None

    def process_message(self, payload: str) -> bool:
        """
        Ingests and processes a raw JSON payload string.
        Routes to the optimized C++ core or executes an allocation-minimal Python fallback scan.
        """
        if self._engine:
            return bool(self._engine.process_json(payload))
        
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # PURE-PYTHON FALLBACK IMPLEMENTATION
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        t_start = time.perf_counter_ns()
        self._stats.received_total += 1
        
        try:
            # Simple string token extraction mimicking the speed of our custom C++ scanner
            def extract_val(key: str) -> str:
                idx = payload.find(key)
                if idx == -1:
                    return ""
                start = payload.find('"', payload.find(':', idx))
                if start == -1:
                    return ""
                end = payload.find('"', start + 1)
                return payload[start + 1:end] if end != -1 else ""

            event_type = extract_val('"event_type"')
            if event_type != "last_trade_price":
                return False

            market_sv = extract_val('"market"')
            asset_sv = extract_val('"asset_id"')
            price_sv = extract_val('"price"')
            size_sv = extract_val('"size"')
            side_sv = extract_val('"side"')
            time_sv = extract_val('"timestamp"')

            if not (market_sv and asset_sv and price_sv and size_sv):
                self._stats.parse_errors += 1
                return False

            # Generate lightweight, stable string hashes for IDs
            def hash32(val: str) -> int:
                h = 2166136261
                for char in val.encode('utf-8', 'ignore'):
                    h = ((h ^ char) * 16777619) & 0xFFFFFFFF
                return h

            market_id = self._market_cache.setdefault(market_sv, hash32(market_sv))
            
            # Pack internal dictionary structure
            trade = {
                "market_id": market_id,
                "asset_id": hash32(asset_sv), # Using 32-bit bound safe value for python default indexing maps
                "price": float(price_sv),
                "size": float(size_sv),
                "usd": float(price_sv) * float(size_sv),
                "side": side_sv if side_sv in ("BUY", "SELL") else "UNKNOWN",
                "timestamp_ms": int(time_sv) if time_sv else int(time.time() * 1000)
            }

            # LAYER 2 ANOMALY PRE-FILTER PREDICTIVE FALLBACK MATRIX
            # Score trades from 0 to 3 based on simple heuristics
            score = 0
            if trade["usd"] > 5000.0:  # Mock high-volume metric threshold placement
                score += 1
            if trade["price"] < 0.20:  # Long-shot trade condition
                score += 1

            self._stats.processed_total += 1

            # Distribute to the appropriate mock queue based on the anomaly score
            if score >= 2:
                self._stats.filter_matches += 1
                if len(self._priority_queue) < self._queue_capacity:
                    self._priority_queue.append(trade)
                else:
                    self._stats.push_fail_total += 1
            else:
                if len(self._normal_queue) < self._queue_capacity:
                    self._normal_queue.append(trade)
                else:
                    self._stats.push_fail_total += 1

            # Update queue depth stats metrics
            self._stats.priority_depth = len(self._priority_queue)
            self._stats.normal_depth = len(self._normal_queue)

            # Record processing latency metrics
            duration_us = (time.perf_counter_ns() - t_start) / 1000.0
            if duration_us <= 1.0: self._stats.latency_bucket_0_1us += 1
            elif duration_us <= 10.0: self._stats.latency_bucket_1_10us += 1
            elif duration_us <= 100.0: self._stats.latency_bucket_10_100us += 1
            elif duration_us <= 1000.0: self._stats.latency_bucket_100_1000us += 1
            else: self._stats.latency_bucket_1000us_plus += 1

            return True

        except Exception as ex:
            logger.error(f"Error handling pure-Python processing fallback block: {ex}")
            self._stats.parse_errors += 1
            return False

    def pop_priority(self) -> Optional[Dict[str, Any]]:
        """Pops an anomalous trade from the high-priority lock-free queue."""
        if self._engine:
            trade = self._engine.pop_priority()
            return self._raw_to_dict(trade)
        
        if not self._priority_queue:
            self._stats.pop_empty_total += 1
            return None
        
        trade = self._priority_queue.popleft()
        self._stats.priority_depth = len(self._priority_queue)
        return trade

    def pop_normal(self) -> Optional[Dict[str, Any]]:
        """Pops a standard trade from the normal priority lock-free queue."""
        if self._engine:
            trade = self._engine.pop_normal()
            return self._raw_to_dict(trade)
        
        if not self._normal_queue:
            self._stats.pop_empty_total += 1
            return None
        
        trade = self._normal_queue.popleft()
        self._stats.normal_depth = len(self._normal_queue)
        return trade

    def get_stats(self) -> Optional[Dict[str, Any]]:
        """Retrieves diagnostics counters from the active execution layer instance context."""
        if self._engine:
            stats = self._engine.get_stats()
            return {
                "received_total": stats.received_total,
                "processed_total": stats.processed_total,
                "filter_matches": stats.filter_matches,
                "parse_errors": stats.parse_errors,
                "push_fail_total": stats.push_fail_total,
                "pop_empty_total": stats.pop_empty_total,
                "normal_depth": stats.normal_depth,
                "priority_depth": stats.priority_depth,
                "pool_available": stats.pool_available,
                "latency_bucket_0_1us": stats.latency_bucket_0_1us,
                "latency_bucket_1_10us": stats.latency_bucket_1_10us,
                "latency_bucket_10_100us": stats.latency_bucket_10_100us,
                "latency_bucket_100_1000us": stats.latency_bucket_100_1000us,
                "latency_bucket_1000us_plus": stats.latency_bucket_1000us_plus,
            }
        
        return {
            "received_total": self._stats.received_total,
            "processed_total": self._stats.processed_total,
            "filter_matches": self._stats.filter_matches,
            "parse_errors": self._stats.parse_errors,
            "push_fail_total": self._stats.push_fail_total,
            "pop_empty_total": self._stats.pop_empty_total,
            "normal_depth": self._stats.normal_depth,
            "priority_depth": self._stats.priority_depth,
            "pool_available": self._stats.pool_available,
            "latency_bucket_0_1us": self._stats.latency_bucket_0_1us,
            "latency_bucket_1_10us": self._stats.latency_bucket_1_10us,
            "latency_bucket_10_100us": self._stats.latency_bucket_10_100us,
            "latency_bucket_100_1000us": self._stats.latency_bucket_100_1000us,
            "latency_bucket_1000us_plus": self._stats.latency_bucket_1000us_plus,
        }

    @staticmethod
    def _raw_to_dict(trade: Any) -> Optional[Dict[str, Any]]:
        if trade is None:
            return None
        return {
            "market_id": trade.market_id,
            "asset_id": trade.asset_id,
            "price": trade.price,
            "size": trade.size,
            "usd": trade.usd,
            "side": getattr(trade.side, "name", str(trade.side)),
            "timestamp_ms": trade.timestamp_ms,
        }