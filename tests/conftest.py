"""Test configuration and fixtures."""

import sys
from unittest.mock import MagicMock

# Mock signal_core before any imports of analytics.order_flow
sys.modules['signal_core'] = MagicMock()
sys.modules['signal_core.order_flow'] = MagicMock()

# Mock the function that would be imported
mock_generate_signal_score = MagicMock(return_value={
    "direction": "BUY",
    "metrics": {
        "ofi_1m": 1.0,
        "ofi_5m": 0.5,
        "ofi_15m": 0.3,
        "ofi_1h": 0.1,
        "volume_spike": 1.5
    },
    "confidence": 0.75
})
sys.modules['signal_core.order_flow'].generate_signal_score = mock_generate_signal_score
