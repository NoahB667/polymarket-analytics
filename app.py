import os
import threading
import asyncio
import requests
from flask import Flask, jsonify, request
from telegram import Bot
from dotenv import load_dotenv
import json
from WebSocketOrderBook import WebSocketOrderBook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Flask(__name__)

active_listeners = {}

def send_telegram_alert(chat_id, message):
    if not BOT_TOKEN or not chat_id:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(chat_id, message))
        loop.close()
    except Exception as e:
        print(f"Failed to send alert: {e}")

@app.route('/get-event-details/<slug>', methods=['GET'])
def get_event_details(slug):
    """
    Endpoint to convert a human-readable slug into Polymarket details.
    Example: http://127.0.0.1:5000/get-event/will-bitcoin-hit-100k
    """
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"

    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return jsonify({"error": "Event slug not found"}), 404

        event = data[0]
        market_info = []

        for market in event.get('markets', []):
            question = market.get("question") or "N/A"
            token_ids = market.get('clobTokenIds')

            if token_ids is None:
                parsed = []
            elif isinstance(token_ids, (list, tuple)):
                parsed = [str(x) for x in token_ids]
            else:
                if isinstance(token_ids, str):
                    s = token_ids.strip()
                    try:
                        decoded = json.loads(s)
                        if isinstance(decoded, (list, tuple)):
                            parsed = [str(x) for x in decoded]
                        else:
                            parsed = [str(decoded)]
                    except Exception:
                        s = s.strip('[]')
                        parsed = [part.strip().strip('"').strip("'") for part in s.split(',') if part.strip()]
                else:
                    parsed = [str(token_ids)]

            market_info.append({"question": question, "clobTokenIds": parsed})

        return jsonify({"title": event.get('title'), "markets": market_info}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get-token-ids/<slug>', methods=['GET'])
def get_token_ids(slug):
    """
    Endpoint to convert a human-readable slug into Polymarket Token IDs.
    Example: http://127.0.0.1:5000/get-event/will-bitcoin-hit-100k
    """
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"

    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return jsonify({"error": "Event slug not found"}), 404

        event = data[0]
        all_token_ids = []

        for market in event.get('markets', []):
            token_ids = market.get('clobTokenIds')

            if token_ids is None:
                parsed = []
            elif isinstance(token_ids, (list, tuple)):
                parsed = [str(x) for x in token_ids]
            else:
                if isinstance(token_ids, str):
                    s = token_ids.strip()
                    try:
                        decoded = json.loads(s)
                        if isinstance(decoded, (list, tuple)):
                            parsed = [str(x) for x in decoded]
                        else:
                            parsed = [str(decoded)]
                    except Exception:
                        s = s.strip('[]')
                        parsed = [part.strip().strip('"').strip("'") for part in s.split(',') if part.strip()]
                else:
                    parsed = [str(token_ids)]

            all_token_ids.extend(parsed)

        return jsonify(all_token_ids), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get-live-trades/<slug>', defaults={'limit': 0})
@app.route('/get-live-trades/<slug>/<limit>')
def get_live_trades(slug, limit):
    chat_id = request.args.get('chat_id')
    token_response, status_code = get_token_ids(slug)
    if status_code != 200:
        return token_response, status_code

    assets_ids = token_response.get_json()

    def on_trade_callback(message_text):
        if chat_id:
            send_telegram_alert(chat_id, message_text)

    listener_key = f"{chat_id}_{slug}"
    if listener_key in active_listeners:
        try:
            active_listeners[listener_key].close()
        except Exception as e:
            print(f"Error closing existing listener: {e}")
        del active_listeners[listener_key]

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    market_connection = WebSocketOrderBook(
        "market", url, assets_ids, on_trade_callback, True, min_size_usd=limit
    )
    active_listeners[listener_key] = market_connection
    def run_websocket():
        market_connection.run()
        if listener_key in active_listeners and active_listeners[listener_key] == market_connection:
            del active_listeners[listener_key]

    thread = threading.Thread(target=run_websocket)
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": f"Started listening for live trades for {slug} with limit {limit}",
        "recipient": chat_id or "Console only"
    }), 200

@app.route('/untrack/<slug>', methods=['GET'])
def untrack_market(slug):
    chat_id = request.args.get('chat_id')
    if not chat_id:
        return jsonify({"error": "chat_id is required"}), 400

    listener_key = f"{chat_id}_{slug}"

    if listener_key in active_listeners:
        try:
            active_listeners[listener_key].close()
            if listener_key in active_listeners:
                del active_listeners[listener_key]
            return jsonify({"message": f"Stopped tracking {slug}"}), 200
        except Exception as e:
            return jsonify({"error": f"Error stopping track: {str(e)}"}), 500
    else:
        return jsonify({"message": f"Not currently tracking {slug}"}), 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)