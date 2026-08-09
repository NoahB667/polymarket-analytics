import os
import requests
import logging
from websocket import WebSocketApp
import json
import time
import threading
from typing import Any, Dict, List, Optional, Set

import redis
from core.cpp_bridge import CoreEngineBridge

MARKET_CHANNEL = "market"

def _build_redis_client() -> Optional[redis.Redis]:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        client = redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


def get_market_metadata(market: str, redis_client: Optional[redis.Redis]) -> Dict[str, object]:
    question_key = f"meta:question:{market}"
    outcomes_hash_key = f"meta:outcomes:{market}"

    if redis_client is not None:
        try:
            # 1. Fetch both the question string and the outcomes hash in parallel pipelines
            pipe = redis_client.pipeline()
            pipe.get(question_key)
            pipe.hgetall(outcomes_hash_key)
            cached_question, cached_outcomes = pipe.execute()

            if cached_question and cached_outcomes:
                return {
                    "question": cached_question,
                    "outcomes": cached_outcomes,
                }
        except Exception as e:
            logging.getLogger("polymarket.core").error(f"Redis cache lookup exception: {e}")

    # Fallback to Live REST API if cache miss occurs
    url = f"https://clob.polymarket.com/markets/{market}"
    try:
        response = requests.get(url, timeout=2.0)
        response.raise_for_status()
        data = response.json()
        question = data.get("question", "N/A")
        outcomes = {}
        for token in data.get("tokens", []):
            token_id = str(token.get("token_id"))
            outcomes[token_id] = token.get("outcome", "N/A")

        if redis_client is not None:
            try:
                # 2. Write the fresh data back using an atomic pipeline
                pipe = redis_client.pipeline()
                pipe.setex(question_key, 86400, question)
                pipe.delete(outcomes_hash_key) # Clear old mapping
                pipe.hset(outcomes_hash_key, mapping=outcomes)
                pipe.expire(outcomes_hash_key, 86400)
                pipe.execute()
            except Exception:
                pass

        return {"question": question, "outcomes": outcomes}
    except Exception:
        return {"question": "N/A", "outcomes": {}}


class WebSocketOrderBook:
    """Orchestrates incoming high-throughput WebSocket streams into the native C++ filtration core."""

    def __init__(
        self,
        channel_type: str,
        url: str,
        data: List[str],
        message_callback: Any,
        verbose: bool,
        min_size_usd: float = 0.0,
        redis_client: Optional[redis.Redis] = None,
        slug: Optional[str] = None,
    ):
        self.channel_type = channel_type
        self.url = url
        self.data = data
        self.message_callback = message_callback
        self.verbose = verbose
        self.min_size_usd = float(min_size_usd)
        self.redis_client = redis_client or _build_redis_client()
        self.slug = slug
        
        # Instantiate strict C++ execution extension wrapper
        self.cpp_engine = CoreEngineBridge()
        
        # Internal state metrics & tracking allocations
        self._hash_to_string_cache: Dict[int, str] = {}
        self._cache_lock = threading.Lock()
        self._known_markets: Set[str] = set()
        self._active_subscribers: Dict[int, Dict[int, float]] = {}
        self._latest_subscriptions: Dict[int, float] = {}
        
        # Pre-seed internal lookup hashes if data markers are present
        if self.data:
            with self._cache_lock:
                for asset_id in self.data:
                    a_str = str(asset_id)
                    self._hash_to_string_cache[self._hash64(a_str)] = a_str
                    
        # Synchronize active profile matrix from historical cache entries
        if self.slug and self.redis_client:
            try:
                subs = self.redis_client.hgetall(f"subscriptions:{self.slug}")
                for cid_str, lim_str in subs.items():
                    try:
                        self._latest_subscriptions[int(cid_str)] = float(lim_str)
                    except ValueError:
                        pass
            except Exception as e:
                if self.verbose:
                    print(f"Error loading initial subscriptions: {e}")
                
        # Fire background ingestion thread
        self._running = True
        self._consumer_thread = threading.Thread(target=self._consumer_loop, daemon=True)
        self._consumer_thread.start()
        
        self.ws = WebSocketApp(
            url=self.url,
            on_message=self.on_message,
            on_open=self.on_open,
        )

    @staticmethod
    def _hash32(val: str) -> int:
        h = 2166136261
        for char in val.encode('utf-8', 'ignore'):
            h = ((h ^ char) * 16777619) & 0xFFFFFFFF
        return h

    @staticmethod
    def _hash64(val: str) -> int:
        h = 1469598103934665603
        for char in val.encode('utf-8', 'ignore'):
            h = ((h ^ char) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return h

    @staticmethod
    def _extract_field_fast(payload: str, field_key: str) -> str:
        """Fast low-overhead index-based slice extraction avoiding regex execution engine costs."""
        idx = payload.find(field_key)
        if idx == -1:
            return ""
        start = payload.find('"', payload.find(':', idx))
        if start == -1:
            return ""
        end = payload.find('"', start + 1)
        return payload[start + 1:end] if end != -1 else ""

    def _process_single_message(self, message: str):
        market_sv = self._extract_field_fast(message, '"market"')
        asset_sv = self._extract_field_fast(message, '"asset_id"')
        
        if market_sv and asset_sv:
            m_hash = self._hash32(market_sv)
            a_hash = self._hash64(asset_sv)
            
            with self._cache_lock:
                self._hash_to_string_cache[m_hash] = market_sv
                self._hash_to_string_cache[a_hash] = asset_sv
                
                if market_sv not in self._known_markets:
                    self._known_markets.add(market_sv)
                    old_subs = self._active_subscribers.setdefault(m_hash, {})
                    for chat_id, min_usd in self._latest_subscriptions.items():
                        self.cpp_engine.update_subscription(chat_id, m_hash, min_usd)
                        old_subs[chat_id] = min_usd
        
        self.cpp_engine.process_message(message)

    def sync_subscriptions(self):
        """Syncs active subscriptions from Redis and propagates updates down to the native layer."""
        if not self.slug or not self.redis_client:
            return
            
        try:
            subscribers = self.redis_client.hgetall(f"subscriptions:{self.slug}")
            new_subs = {}
            for chat_id_str, limit_str in subscribers.items():
                try:
                    new_subs[int(chat_id_str)] = float(limit_str)
                except ValueError:
                    continue
                    
            with self._cache_lock:
                self._latest_subscriptions = new_subs
                for market_sv in self._known_markets:
                    m_hash = self._hash32(market_sv)
                    old_subs = self._active_subscribers.setdefault(m_hash, {})
                    
                    # Evict dropped keys
                    for old_chat_id in list(old_subs.keys()):
                        if old_chat_id not in new_subs:
                            self.cpp_engine.remove_subscription(old_chat_id, m_hash)
                            old_subs.pop(old_chat_id, None)
                            
                    # Propagate inserts/modifications
                    for chat_id, min_usd in new_subs.items():
                        if old_subs.get(chat_id) != min_usd:
                            self.cpp_engine.update_subscription(chat_id, m_hash, min_usd)
                            old_subs[chat_id] = min_usd
        except Exception as e:
            if self.verbose:
                print(f"Error syncing subscriptions: {e}")

    def on_message(self, ws, message: str):
        try:
            stripped = message.lstrip()
            if not stripped:
                return

            # Native Array/List batch unpacking route
            if stripped.startswith("["):
                try:
                    data = json.loads(message)
                    messages = data if isinstance(data, list) else [data]
                except json.JSONDecodeError:
                    return
                for msg in messages:
                    if isinstance(msg, dict):
                        self._process_single_message(json.dumps(msg))
            else:
                self._process_single_message(message)
                
        except Exception as e:
            if self.verbose:
                print(f"Error processing message framework packet: {e}")

    def _consumer_loop(self):
        while self._running:
            try:
                # Always deplete prioritized anomaly queues before checking regular trades
                trade = self.cpp_engine.pop_priority()
                if not trade:
                    trade = self.cpp_engine.pop_normal()
                    
                if not trade:
                    time.sleep(0.001)
                    continue
                    
                market_hash = trade.get("market_id")
                asset_hash = trade.get("asset_id")
                
                with self._cache_lock:
                    market_id = self._hash_to_string_cache.get(market_hash)
                    asset_id = self._hash_to_string_cache.get(asset_hash)
                    
                if not market_id or not asset_id:
                    continue
                    
                metadata = get_market_metadata(market_id, self.redis_client)
                question = metadata.get("question", "N/A")
                outcome = metadata.get("outcomes", {}).get(str(asset_id), "N/A")
                
                if self.message_callback:
                    usd = float(trade.get("usd", 0.0))
                    if usd < self.min_size_usd:
                        continue
                        
                    price = float(trade.get("price", 0.0))
                    size = float(trade.get("size", 0.0))
                    side = trade.get("side", "UNKNOWN")
                    
                    text = f"{side} @ {price} ({usd:.2f}$), {question} {outcome}"
                    details = {
                        "slug": self.slug,
                        "market": market_id,
                        "asset_id": asset_id,
                        "price": price,
                        "size": size,
                        "usd": usd,
                        "side": side,
                        "question": question,
                        "outcome": outcome,
                        "text": text,
                        "raw": trade,
                    }
                    self.message_callback(details)
            except Exception as e:
                print(f"Error inside background consumer loop: {e}")

    def on_open(self, ws):
        if self.channel_type == MARKET_CHANNEL:
            ws.send(json.dumps({"assets_ids": self.data, "type": MARKET_CHANNEL}))
        else:
            self.ws.close()
            return

        thr = threading.Thread(target=self._send_ping, args=(ws,), daemon=True)
        thr.start()

    def _send_ping(self, ws):
        while self.ws.sock and self.ws.sock.connected:
            try:
                ws.send("PING")
                time.sleep(5)
            except Exception:
                break

    def run(self):
        while self._running:
            try:
                self.ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                )
            except Exception as e:
                print(f"WebSocket runtime crashed: {e}")

            if not self._running:
                break

            print("Reconnecting in 5s...")
            time.sleep(5)
            self.ws = WebSocketApp(
                url=self.url,
                on_message=self.on_message,
                on_open=self.on_open,
            )

    def close(self):
        self._running = False
        if self.ws:
            self.ws.close()