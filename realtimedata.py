from websocket import WebSocketApp
import json
import time
import threading

MARKET_CHANNEL = "market"

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

if __name__ == "__main__":
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    assets_ids = [
            "11862165566757345985240476164489718219056735011698825377388402888080786399275",
            "71478852790279095447182996049071040792010759617668969799049179229104800573786",
            "92703761682322480664976766247614127878023988651992837287050266308961660624165",
            "48193521645113703700467246669338225849301704920590102230072263970163239985027",
            "112838095111461683880944516726938163688341306245473734071798778736646352193304",
            "7321318078891059430231591636389479745928915782241484131001985601124919020061",
            "16419649354067298412736919830777830730026677464626899811394461690794060330642",
            "42139849929574046088630785796780813725435914859433767469767950066058132350666"
    ]
    market_connection = WebSocketOrderBook(
        MARKET_CHANNEL, url, assets_ids, None, True
    )
    market_connection.run()
