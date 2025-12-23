from datetime import datetime
from zoneinfo import ZoneInfo
import os
import requests
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
import json

load_dotenv()

base_url = os.getenv("BASE_URL")
markets_endpoint = os.getenv("MARKETS_ENDPOINT")
events_endpoint = os.getenv("EVENTS_ENDPOINT")
whale_threshold = os.getenv("WHALE_THRESHOLD")

# Flask
app = Flask(__name__, template_folder="templates")

params = {
    'order': 'id',
    'ascending': 'false',
    'closed': 'false',
    'limit': '20'
}

def fetch_active_markets():
    try:
        response = requests.get(base_url + markets_endpoint, params=params)
        response.raise_for_status()
        markets = response.json()
    except requests.exceptions.HTTPError as http_err:
        return jsonify({'error': f'HTTP error occurred: {http_err}'}), 500
    except Exception as err:
        return jsonify({'error': f'Other error occurred: {err}'}), 500
    return markets

def fetch_active_events():
    try:
        response = requests.get(base_url + events_endpoint, params=params)
        response.raise_for_status()
        events = response.json()
    except requests.exceptions.HTTPError as http_err:
        return jsonify({'error': f'HTTP error occurred: {http_err}'}), 500
    except Exception as err:
        return jsonify({'error': f'Other error occurred: {err}'}), 500
    return events

def utc_to_est(utc_iso_str):
    if not utc_iso_str:
        return "N/A"
    try:
        s = str(utc_iso_str).strip()
        # Normalize Z to +00:00 so fromisoformat can parse it
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        # If naive (no tzinfo), assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        est_time = dt.astimezone(ZoneInfo("US/Eastern"))
        return est_time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "N/A"

def fetch_trump_markets():
    response = fetch_active_markets()
    trump_markets = {}
    for market in response:
        if 'Trump' in market.get('data', {}).get('title', ''):
            trump_markets[market['id']] = market
    return trump_markets

def filter_markets_by_tag_slugs(markets, desired_slugs):
    desired = {s.lower() for s in desired_slugs}
    filtered = []
    for m in markets:
        tags = m.get('tags', []) if isinstance(m, dict) else []
        slugs = {t.get('slug', '').lower() for t in tags if isinstance(t, dict)}
        if slugs & desired:  # intersection (OR logic)
            filtered.append(m)
    return filtered

# Routes
@app.route('/')
def index():
    markets = fetch_active_markets()
    return render_template("index.html", markets=markets, utc_to_est=utc_to_est)

@app.route('/markets')
def markets():
    markets = fetch_active_markets()
    return render_template("index.html", markets=markets, utc_to_est=utc_to_est)

@app.route('/markets/trump')
def trump():
    return fetch_trump_markets()

@app.route('/events')
def events():
    return fetch_active_events()

@app.route('/markets/categories')
def markets_by_categories():
    # Query param example: /markets/categories?tags=politics,geopolitics
    raw = request.args.get('tags', '')
    if not raw:
        return jsonify({'error': 'Provide tags query param, e.g. ?tags=politics,geopolitics'}), 400
    desired_slugs = [s.strip() for s in raw.split(',') if s.strip()]
    markets = fetch_active_markets()
    # If error response came back, pass it through
    if not isinstance(markets, list):
        return markets
    filtered = filter_markets_by_tag_slugs(markets, desired_slugs)
    return jsonify(filtered)

@app.route('/get-event/<slug>', methods=['GET'])
def get_event_details(slug):
    """
    Endpoint to convert a human-readable slug into Polymarket Token IDs.
    Example: http://127.0.0.1:5000/get-event/will-bitcoin-hit-100k
    """
    gamma_url = f"https://gamma-api.polymarket.com/events?slug={slug}"

    try:
        response = requests.get(gamma_url, timeout=10)
        response.raise_for_status()  # Check for HTTP errors
        data = response.json()
        if not data:
            return jsonify({"error": "Event slug not found"}), 404

        # Extracting the core data for your Whale Tracker
        event = data[0]
        market_info = []

        for market in event.get('markets', []):
            question = market.get("question") or "N/A"
            token_ids = market.get('clobTokenIds')
            parsed = []

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


if __name__ == '__main__':
    app.run(debug=True, port=5000)