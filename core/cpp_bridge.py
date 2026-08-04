"""C++ bridge for high-performance trade processing with strict native execution."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("polymarket.core.cpp_bridge")

try:
    from signal_core.trade_filter import LONG_SHOT_PRICE_THRESHOLD, LARGE_TRADE_USD_THRESHOLD
except ImportError as e:
    logger.critical("Fatal: signal_core package not found or failed to load. "
                    "Run: git submodule update --init && pip install -e vendor/signal-core")
    raise ImportError("signal_core package missing. Aborting startup.") from e

try:
    import polymarket_core as _cpp
    _HAS_CPP = True
except ImportError as e:
    _cpp = None
    _HAS_CPP = False
    logger.critical("Fatal: polymarket_core C++ extension binary not found or failed to load. "
                    "Production builds require the native extension compiled.")
    raise ImportError("polymarket_core C++ binary missing. Aborting startup.") from e


class CoreEngineBridge:
    """Strict wrapper around the C++ CoreEngine providing zero-allocation pass-through optimization."""

    def __init__(self, pool_capacity: int = 1024, queue_capacity: int = 1024) -> None:
        self._pool_capacity = pool_capacity
        self._queue_capacity = queue_capacity
        
        try:
            self._engine = _cpp.CoreEngine(
                pool_capacity,
                queue_capacity,
                LONG_SHOT_PRICE_THRESHOLD,
                LARGE_TRADE_USD_THRESHOLD,
            )
        except Exception as e:
            logger.critical(f"Failed to instantiate C++ CoreEngine within runtime context: {e}")
            raise

    def is_cpp_available(self) -> bool:
        """Returns True if the high-performance C++ shared object engine is actively bound."""
        return True

    def update_subscription(self, chat_id: int, market_hash: int, min_usd: float) -> None:
        """Updates or inserts a user routing threshold profile rule inside the active C++ filter engine."""
        self._engine.update_subscription(int(chat_id), int(market_hash), float(min_usd))

    def remove_subscription(self, chat_id: int, market_hash: int) -> None:
        """Deletes a user tracking rule entirely from the C++ engine tracking matrix."""
        self._engine.remove_subscription(int(chat_id), int(market_hash))

    def process_message(self, payload: str) -> bool:
        """
        Ingests and processes a raw JSON payload string directly into the C++ parser loop.
        Returns True if the trade is successfully parsed and pushed to a queue.
        """
        return bool(self._engine.process_json(payload))

    def pop_priority(self) -> Optional[Dict[str, Any]]:
        """Pops an anomalous trade from the high-priority lock-free queue."""
        trade = self._engine.pop_priority()
        return self._raw_to_dict(trade)

    def pop_normal(self) -> Optional[Dict[str, Any]]:
        """Pops a standard trade from the normal priority lock-free queue."""
        trade = self._engine.pop_normal()
        return self._raw_to_dict(trade)

    def get_stats(self) -> Dict[str, Any]:
        """Retrieves diagnostics counters from the active C++ core snapshot instance."""
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

    @staticmethod
    def _raw_to_dict(trade: Any) -> Optional[Dict[str, Any]]:
        if trade is None:
            return None
            
        if hasattr(trade.side, "name"):
            side_str = str(trade.side.name).upper()
        else:
            side_str = str(trade.side).upper()
            
        if "BUY" in side_str:
            normalized_side = "BUY"
        elif "SELL" in side_str:
            normalized_side = "SELL"
        else:
            normalized_side = "UNKNOWN"

        return {
            "market_id": trade.market_id,
            "asset_id": trade.asset_id,
            "price": trade.price,
            "size": trade.size,
            "usd": trade.usd,
            "side": normalized_side,
            "timestamp_ms": trade.timestamp_ms,
        }