"""C++ bridge for high-performance trade processing with full pure-Python fallbacks."""

import time
import logging
from collections import deque
from dataclasses import dataclass
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
            # Fallback memory matrix: Dict[market_id, Dict[chat_id, min_usd]]
            self._fallback_subscriptions: Dict[int, Dict[int, float]] = {}

    def is_cpp_available(self) -> bool:
        """Returns True if the high-performance C++ shared object engine is actively bound."""
        return self._engine is not None

    def update_subscription(self, chat_id: int, market_hash: int, min_usd: float) -> None:
        """
        Updates or inserts a user routing threshold profile rule inside the active filter engine.
        
        Args:
            chat_id: Unique identifying integer tracking the target user destination.
            market_hash: Stable hash identity identifier of the target prediction pool.
            min_usd: The minimum dollar limit execution volume required to trigger a matching event.
        """
        if self._engine:
            self._engine.update_subscription(chat_id, market_hash, min_usd)
        else:
            # Mirror performance changes inside local state tables if C++ engine is absent
            if market_hash not in self._fallback_subscriptions:
                self._fallback_subscriptions[market_hash] = {}
            self._fallback_subscriptions[market_hash][chat_id] = min_usd
            logger.debug(f"[Fallback Sync] Updated subscription: Market={market_hash}, User={chat_id}, Limit=${min_usd}")

    def remove_subscription(self, chat_id: int, market_hash: int) -> None:
        """
        Deletes a user tracking rule entirely from the engine tracking matrix.
        
        Args:
            chat_id: Unique identifying integer tracking the target user destination.
            market_hash: Stable hash identity identifier of the target prediction pool.
        """
        if self._engine:
            self._engine.remove_subscription(chat_id, market_hash)
        else:
            # Purge entry points safely from local python structures
            if market_hash in self._fallback_subscriptions:
                self._fallback_subscriptions[market_hash].pop(chat_id, None)
                if not self._fallback_subscriptions[market_hash]:
                    self._fallback_subscriptions.pop(market_hash)
            logger.debug(f"[Fallback Sync] Removed subscription: Market={market_hash}, User={chat_id}")

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

            def hash32(val: str) -> int:
                h = 2166136261
                for char in val.encode('utf-8', 'ignore'):
                    h = ((h ^ char) * 16777619) & 0xFFFFFFFF
                return h

            market_id = self._market_cache.setdefault(market_sv, hash32(market_sv))
            trade_usd = float(price_sv) * float(size_sv)
            
            trade = {
                "market_id": market_id,
                "asset_id": hash32(asset_sv),
                "price": float(price_sv),
                "size": float(size_sv),
                "usd": trade_usd,
                "side": side_sv if side_sv in ("BUY", "SELL") else "UNKNOWN",
                "timestamp_ms": int(time_sv) if time_sv else int(time.time() * 1000)
            }

            # LAYER 2 ANOMALY PRE-FILTER PREDICTIVE FALLBACK MATRIX WITH ROUTING MATCH CHECKS
            score = 0
            if trade_usd > 5000.0:
                score += 1
            if trade["price"] < 0.20:
                score += 1
                
            # Cross-examine local subscription tables to identify threshold crossings
            has_matching_subscriber = False
            if market_id in self._fallback_subscriptions:
                for uid, min_limit in self._fallback_subscriptions[market_id].items():
                    if trade_usd >= min_limit:
                        has_matching_subscriber = True
                        break

            if has_matching_subscriber:
                score += 1  # Route to priority queue if an active subscriber limit is triggered

            self._stats.processed_total += 1

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

            self._stats.priority_depth = len(self._priority_queue)
            self._stats.normal_depth = len(self._normal_queue)

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
            "side": getattr(trade.side, "name", str(trade.side).upper()),
            "timestamp_ms": trade.timestamp_ms,
        }