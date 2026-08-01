import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from blockchain.market_resolution_client import MarketResolution, MarketResolutionClient


RESOLVED_MARKET_RESPONSE = {
    "closed": True,
    "end_date_iso": "2023-03-15T00:00:00Z",
    "tokens": [
        {"outcome": "Arizona State", "price": 1, "winner": True},
        {"outcome": "Nevada", "price": 0, "winner": False},
    ],
}

UNRESOLVED_50_50_RESPONSE = {
    "closed": True,
    "end_date_iso": "2023-04-10T23:59:59Z",
    "tokens": [
        {"outcome": "Yes", "price": 0.5, "winner": False},
        {"outcome": "No", "price": 0.5, "winner": False},
    ],
}


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_returns_winner_and_end_time(mock_get):
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = RESOLVED_MARKET_RESPONSE
    mock_get.return_value = mock_response

    client = MarketResolutionClient()
    result = client.resolve_market("0xabc")

    assert result.resolved_outcome == "Arizona State"
    assert result.market_end_time is not None


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_no_winner_token_returns_none_outcome(mock_get):
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = UNRESOLVED_50_50_RESPONSE
    mock_get.return_value = mock_response

    client = MarketResolutionClient()
    result = client.resolve_market("0xabc")

    assert result.resolved_outcome is None
    assert result.market_end_time is not None


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_404_returns_none_fields_no_raise(mock_get):
    mock_get.return_value = MagicMock(status_code=404)

    client = MarketResolutionClient()
    result = client.resolve_market("0xdeadbeef")

    assert result == MarketResolution(resolved_outcome=None, market_end_time=None)


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_network_error_returns_none_fields_no_raise(mock_get):
    mock_get.side_effect = ConnectionError("boom")

    client = MarketResolutionClient()
    result = client.resolve_market("0xabc")

    assert result == MarketResolution(resolved_outcome=None, market_end_time=None)


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_caches_by_condition_id(mock_get):
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = RESOLVED_MARKET_RESPONSE
    mock_get.return_value = mock_response

    client = MarketResolutionClient()
    client.resolve_market("0xabc")
    client.resolve_market("0xabc")

    assert mock_get.call_count == 1


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_malformed_end_date_does_not_block_outcome_parsing(mock_get):
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {
        "closed": True,
        "end_date_iso": 12345,  # malformed: not a string
        "tokens": [{"outcome": "Arizona State", "price": 1, "winner": True}],
    }
    mock_get.return_value = mock_response

    client = MarketResolutionClient()
    result = client.resolve_market("0xabc")

    assert result.resolved_outcome == "Arizona State"
    assert result.market_end_time is None


@patch("blockchain.market_resolution_client.requests.get")
def test_resolve_market_http_500_returns_none_fields_no_raise(mock_get):
    mock_response = MagicMock(status_code=500)
    mock_response.raise_for_status.side_effect = requests.HTTPError("server error")
    mock_get.return_value = mock_response

    client = MarketResolutionClient()
    result = client.resolve_market("0xabc")

    assert result == MarketResolution(resolved_outcome=None, market_end_time=None)
