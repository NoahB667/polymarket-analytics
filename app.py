from datetime import datetime
from zoneinfo import ZoneInfo
import os
import requests
import json
from flask import Flask, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

base_url = os.getenv("BASE_URL")
markets_endpoint = os.getenv("MARKETS_ENDPOINT")
whale_threshold = os.getenv("WHALE_THRESHOLD")

# Flask
app = Flask(__name__, template_folder="templates")

params = {
    'order': 'id',
    'ascending': 'false',
    'closed': 'false',
    'limit': 100,
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

def utc_to_est(utc_iso_str):
    if utc_iso_str.endswith('Z'):
        utc_iso_str = utc_iso_str[:-1]
    utc_time = datetime.fromisoformat(utc_iso_str).replace(tzinfo=ZoneInfo("UTC"))
    est_time = utc_time.astimezone(ZoneInfo("US/Eastern"))
    return est_time.strftime("%Y-%m-%d %H:%M:%S")

# Routes
@app.route('/')
def index():
    markets = fetch_active_markets()
    return render_template("index.html", markets=markets, utc_to_est=utc_to_est)

@app.route('/markets')
def markets():
    return fetch_active_markets()

if __name__ == '__main__':
    app.run(debug=True)