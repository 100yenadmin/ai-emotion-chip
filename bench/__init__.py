"""Benches for the plugin in plugin/; any bench entry point can import its emotion_chip package."""
import sys
from pathlib import Path

_PLUGIN = str(Path(__file__).resolve().parents[1] / "plugin")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)
