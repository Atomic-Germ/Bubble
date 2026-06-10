import importlib.util
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

# The daemon source file uses a hyphen in its name (bubble-daemon.py),
# which is not a valid Python module name. Load it explicitly.
_DAEMON_FILE = Path(__file__).resolve().parent.parent / "daemon" / "bubble-daemon.py"

_spec = importlib.util.spec_from_file_location("bubble_daemon", _DAEMON_FILE)
bd = importlib.util.module_from_spec(_spec)
sys.modules["bubble_daemon"] = bd
_spec.loader.exec_module(bd)


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A BubbleDaemon with all file paths pointing at a temp directory."""
    monkeypatch.setenv("BUBBLE_CTX", "8192")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bd, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(bd, "MEMORY_BACKUP_DIR", tmp_path / "memory-backups")
    monkeypatch.setattr(bd, "SCRATCH_FILE", tmp_path / "scratch.json")
    monkeypatch.setattr(bd, "SYSTEM_PROMPT_FILE", tmp_path / "system_prompt.txt")
    monkeypatch.setattr(bd, "DAILY_LOG_FILE", tmp_path / "bubble_daily.json")
    # Prevent GLib.idle_add calls from blowing up in tests (no GLib main loop).
    monkeypatch.setattr(bd.GLib, "idle_add", lambda *a, **k: None)

    d = bd.BubbleDaemon()
    d._lock = threading.Lock()
    yield d
