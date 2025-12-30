import os
import threading
import asyncio
import requests
import json
from flask import Flask, jsonify, request
from telegram import Bot
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from WebSocketOrderBook import WebSocketOrderBook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

app = Flask(__name__)

# Database config
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///polymarket.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# In memory state (needed for active WebSocket connections)
active_listeners = {}

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.String(50), nullable=False)
    slug = db.Column(db.String(200), nullable=False)
    limit_usd = db.Column(db.Float, default=0.0)
    # Ensure that user cannot track same slug twice
    __table_args__ = (db.UniqueConstraint('chat_id', 'slug', name='_chat_slug_uc'),)

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

def get_token_ids(slug):
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"

    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return None, "Event slug not found"

        event = data[0]
        all_token_ids = []

        for market in event.get('markets', []):
            token_ids = market.get('clobTokenIds')
            parsed = []

            if token_ids is None:
                pass
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

        return all_token_ids, None

    except Exception as e:
        return None, str(e)

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

def start_listener(chat_id, slug, limit):
    assets_ids, error = get_token_ids(slug)
    if error:
        print(f"Could not start listener for {slug}: {error}")
        return False, error

    def on_trade_callback(message_text):
        if chat_id:
            send_telegram_alert(chat_id, message_text)

    listener_key = f"{chat_id}_{slug}"

    # Close existing listener key if present
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
    return True, "Started"

@app.route('/get-live-trades/<slug>', defaults={'limit': 0})
@app.route('/get-live-trades/<slug>/<limit>')
def get_live_trades(slug, limit):
    chat_id = request.args.get('chat_id')
    if not chat_id:
        return jsonify({"error": "chat_id is required"}), 400
    try:
        limit_val = float(limit)
    except ValueError:
        limit_val = 0.0

    # Save to DB (Persistence)
    try:
        sub = Subscription.query.filter_by(chat_id=str(chat_id), slug=slug).first()
        if sub:
            sub.limit_usd = limit_val
        else:
            sub = Subscription(chat_id=str(chat_id), slug=slug, limit_usd=limit_val)
            db.session.add(sub)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    # Start Listener (runtime)
    success, msg = start_listener(chat_id, slug, limit_val)
    if not success:
        return jsonify({"error": msg}), 400
    return jsonify({
        "message": f"Started listening for {slug} with limit {limit_val}",
        "recipient": chat_id
    }), 200

@app.route('/untrack/<slug>', methods=['GET'])
def untrack_market(slug):
    chat_id = request.args.get('chat_id')
    if not chat_id:
        return jsonify({"error": "chat_id is required"}), 400

    # Remove from DB
    try:
        sub = Subscription.query.filter_by(chat_id=str(chat_id), slug=slug).first()
        if sub:
            db.session.delete(sub)
            db.session.commit()
        else:
            return jsonify({"message": f"Not currently tracking {slug} in DB"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 500

    # Stop listener
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
    with app.app_context():
        db.create_all()
        print("Database initialized.")

        # Restore subscriptions on startup
        subscriptions = Subscription.query.all()
        print(f"Restoring {len(subscriptions)} active subscriptions...")

        for sub in subscriptions:
            print(f"Restarting tracker for {sub.slug} (Chat: {sub.chat_id})")
            start_listener(sub.chat_id, sub.slug, sub.limit_usd)

    is_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    app.run(debug=is_debug, host='0.0.0.0', port=8000)
