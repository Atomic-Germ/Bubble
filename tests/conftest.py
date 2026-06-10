import importlib.util
import sys
from pathlib import Path

# The daemon source file uses a hyphen in its name (bubble-daemon.py),
# which is not a valid Python module name. Load it explicitly.
_DAEMON_FILE = Path(__file__).resolve().parent.parent / "daemon" / "bubble-daemon.py"

_spec = importlib.util.spec_from_file_location("bubble_daemon", _DAEMON_FILE)
bd = importlib.util.module_from_spec(_spec)
sys.modules["bubble_daemon"] = bd
_spec.loader.exec_module(bd)
