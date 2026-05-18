import os
import requests
from websocket import WebSocketApp
import json
import time
import threading
from typing import Dict, Optional

import redis

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
    outcome_prefix = f"meta:outcome:{market}:"

    if redis_client is not None:
        cached_question = redis_client.get(question_key)
        cached_outcomes = {}
        if cached_question:
            for key in redis_client.scan_iter(f"{outcome_prefix}*"):
                asset_id = key.split(":")[-1]
                cached_value = redis_client.get(key)
                if cached_value is not None:
                    cached_outcomes[asset_id] = cached_value
            if cached_outcomes:
                return {
                    "question": cached_question,
                    "outcomes": cached_outcomes,
                }

    url = f"https://clob.polymarket.com/markets/{market}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        question = data.get("question", "N/A")
        outcomes = {}
        for token in data.get("tokens", []):
            token_id = str(token.get("token_id"))
            outcomes[token_id] = token.get("outcome", "N/A")

        if redis_client is not None:
            redis_client.setex(question_key, 86400, question)
            for token_id, outcome in outcomes.items():
                redis_client.setex(f"{outcome_prefix}{token_id}", 86400, outcome)

        return {"question": question, "outcomes": outcomes}
    except Exception:
        return {"question": "N/A", "outcomes": {}}

class WebSocketOrderBook:
    def __init__(
        self,
        channel_type,
        url,
        data,
        message_callback,
        verbose,
        min_size_usd=0,
        redis_client: Optional[redis.Redis] = None,
    ):
        self.channel_type = channel_type
        self.url = url
        self.data = data
        self.message_callback = message_callback
        self.verbose = verbose
        self.min_size_usd = float(min_size_usd)
        self.redis_client = redis_client or _build_redis_client()
        self.ws = WebSocketApp(
            url=self.url,
            on_message=self.on_message,
            on_open=self.on_open,
        )
        self.orderbooks = {}

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            messages = data if isinstance(data, list) else [data]
            for msg in messages:
                if isinstance(msg, dict):
                    event_type = msg.get("event_type")
                    if event_type in ["last_trade_price"]:
                        print(json.dumps(msg, indent=2))
                        if self.message_callback:
                            price = msg.get("price", "0")
                            size = msg.get("size", "0")
                            usd = float(size) * float(price)

                            if usd < self.min_size_usd:
                                continue

                            market_id = msg.get("market")
                            metadata = get_market_metadata(market_id, self.redis_client)
                            question = metadata.get("question", "N/A")
                            outcome = metadata.get("outcomes", {}).get(
                                str(msg.get("asset_id")),
                                "N/A",
                            )
                            side = msg.get("side", "?")
                            text = f"{side} @ {price} ({usd:.2f}$), {question} {outcome}"
                            if self.message_callback:
                                details = {
                                    "market": market_id,
                                    "asset_id": msg.get("asset_id"),
                                    "price": float(price),
                                    "size": float(size),
                                    "usd": usd,
                                    "side": side,
                                    "question": question,
                                    "outcome": outcome,
                                    "text": text,
                                    "raw": msg,
                                }
                                self.message_callback(details)

        except json.JSONDecodeError:
            print(f"Received non-JSON message: {message}")
        except Exception as e:
            print(f"Error processing message: {e}")

    def on_open(self, ws):
        if self.channel_type == MARKET_CHANNEL:
            ws.send(json.dumps({"assets_ids": self.data, "type": MARKET_CHANNEL}))
        else:
            self.ws.close()
            return

        thr = threading.Thread(target=self.ping, args=(ws,))
        thr.start()

    def ping(self, ws):
        while self.ws.sock and self.ws.sock.connected:
            try:
                ws.send("PING")
                time.sleep(5)
            except Exception as e:
                print(f"Ping error: {e}")

    def run(self):
        self.ws.run_forever()

    def close(self):
        if self.ws:
            self.ws.close()
