from datetime import datetime
from zoneinfo import ZoneInfo
import os
import requests
import json
from flask import Flask, jsonify, render_template
from dotenv import load_dotenv
import websockets
import asyncio

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
    'limit': 10,
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


if __name__ == '__main__':
    app.run(debug=True)