import sys
from pathlib import Path
from unittest.mock import patch

root_path = Path(__file__).resolve().parents[2]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import core.auto_discovery as auto_discovery


def test_check_disk_usage_computes_percentage():
    with patch("core.auto_discovery.shutil.disk_usage", return_value=(100 * 1024**3, 70 * 1024**3, 30 * 1024**3)):
        result = auto_discovery.check_disk_usage()
    assert result["used_pct"] == 70.0
    assert result["used_gb"] == 70.0
    assert result["total_gb"] == 100.0


def test_disk_gate_level_boundaries():
    assert auto_discovery.disk_gate_level(69.9) == "normal"
    assert auto_discovery.disk_gate_level(70.0) == "warning"
    assert auto_discovery.disk_gate_level(80.0) == "alert"
    assert auto_discovery.disk_gate_level(90.0) == "critical"
