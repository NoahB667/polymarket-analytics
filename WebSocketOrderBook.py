import requests
from websocket import WebSocketApp
import json
import time
import threading

MARKET_CHANNEL = "market"

def get_question(market):
    url = f"https://clob.polymarket.com/markets/{market}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("question", "N/A")
    except Exception as e:
        return f"Error: {str(e)}"

def get_outcome(market, asset_id):
    url = f"https://clob.polymarket.com/markets/{market}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        for token in data.get("tokens", []):
            if asset_id == token.get("token_id"):
                return token.get("outcome", "N/A")
        return "Outcome not found"
    except Exception as e:
        return f"Error: {str(e)}"

class WebSocketOrderBook:
    def __init__(self, channel_type, url, data, message_callback, verbose):
        self.channel_type = channel_type
        self.url = url
        self.data = data
        self.message_callback = message_callback
        self.verbose = verbose
        self.ws = WebSocketApp(
            url=self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
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
                            question = get_question(msg.get("market"))
                            outcome = get_outcome(msg.get("market"), msg.get("asset_id"))
                            price = msg.get("price", "?")
                            size = msg.get("size", "?")
                            side = msg.get("side", "?")
                            usd = float(size) * float(price)
                            text = f"{side} @ {price} ({usd:.2f}$), {question} {outcome}"
                            self.message_callback(text)

        except json.JSONDecodeError:
            print(f"Received non-JSON message: {message}")
        except Exception as e:
            print(f"Error processing message: {e}")

    def on_error(self, ws, error):
        print("Error: ", error)
        exit(1)

    def on_close(self, ws, close_status_code, close_msg):
        print("closing")
        exit(0)

    def on_open(self, ws):
        if self.channel_type == MARKET_CHANNEL:
            ws.send(json.dumps({"assets_ids": self.data, "type": MARKET_CHANNEL}))
        else:
            exit(1)

        thr = threading.Thread(target=self.ping, args=(ws,))
        thr.start()

    def subscribe_to_tokens_ids(self, assets_ids):
        if self.channel_type == MARKET_CHANNEL:
            self.ws.send(json.dumps({"assets_ids": assets_ids, "operation": "subscribe"}))

    def unsubscribe_to_tokens_ids(self, assets_ids):
        if self.channel_type == MARKET_CHANNEL:
            self.ws.send(json.dumps({"assets_ids": assets_ids, "operation": "unsubscribe"}))

    def ping(self, ws):
        while True:
            ws.send("PING")
            time.sleep(5)

    def run(self):
        self.ws.run_forever()
