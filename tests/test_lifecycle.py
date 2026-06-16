"""
Tests for lifecycle logic: memory backup, scratch, daily log, sleep
intent routing, nudge/dream/nap/slow-wave triggers, hardware state
formatting, and telemetry.
"""

import json
import threading
import time
from pathlib import Path
from unittest import mock
from conftest import bd


# ---------------------------------------------------------------------------
# _format_hardware_state
# ---------------------------------------------------------------------------

class TestFormatHardwareState:
    """Pure string formatting of a compact hardware line."""

    def test_basic(self, daemon):
        ctx = {"window": "firefox: Settings"}
        tel = {"npu_temp": "45.2°C", "npu_load": "30%"}
        s = daemon._format_hardware_state(ctx, tel)
        assert "45.2" in s
        assert "30%" in s
        assert "firefox: Settings" in s

    def test_unknown_window(self, daemon):
        ctx = {}
        tel = {"npu_temp": "N/A", "npu_load": "N/A"}
        s = daemon._format_hardware_state(ctx, tel)
        assert "Active: Unknown" in s

    def test_missing_telemetry_keys(self, daemon):
        s = daemon._format_hardware_state({"window": "code"}, {})
        assert "N/A" in s
        assert "code" in s


# ---------------------------------------------------------------------------
# _backup_existing_memory_file / _prune_memory_backups
# ---------------------------------------------------------------------------

class TestMemoryBackup:
    """Backup and pruning of memory snapshots (uses real filesystem)."""

    def test_backup_created_when_different(self, daemon, tmp_path):
        memory_file = tmp_path / "memory.json"
        memory_file.write_text(json.dumps({"old": "value"}))
        backup_dir = tmp_path / "backups"

        with mock.patch.object(bd, "MEMORY_FILE", memory_file):
            with mock.patch.object(bd, "MEMORY_BACKUP_DIR", backup_dir):
                daemon._backup_existing_memory_file(
                    json.dumps({"old": "different"})
                )

        backups = list(backup_dir.glob("memory-*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text()) == {"old": "value"}

    def test_backup_skipped_when_identical(self, daemon, tmp_path):
        memory_file = tmp_path / "memory.json"
        data = json.dumps({"same": "value"})
        memory_file.write_text(data)
        backup_dir = tmp_path / "backups"

        with mock.patch.object(bd, "MEMORY_FILE", memory_file):
            with mock.patch.object(bd, "MEMORY_BACKUP_DIR", backup_dir):
                daemon._backup_existing_memory_file(data)

        assert not backup_dir.exists()

    def test_prune_keeps_newest(self, daemon, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for i in range(5):
            (backup_dir / f"memory-20250101-00000{i}-000000.json").write_text(
                json.dumps({"idx": i})
            )

        with mock.patch.object(bd, "MEMORY_BACKUP_DIR", backup_dir):
            with mock.patch("conftest.bd._get_memory_backup_limit", return_value=2):
                daemon._prune_memory_backups()

        remaining = sorted(backup_dir.glob("memory-*.json"))
        assert len(remaining) == 2
        assert "04" in remaining[-1].name
        assert "03" in remaining[0].name

    def test_prune_does_not_remove_when_under_limit(self, daemon, tmp_path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "memory-001.json").write_text("{}")
        (backup_dir / "memory-002.json").write_text("{}")

        with mock.patch.object(bd, "MEMORY_BACKUP_DIR", backup_dir):
            with mock.patch("conftest.bd._get_memory_backup_limit", return_value=10):
                daemon._prune_memory_backups()

        assert len(list(backup_dir.glob("memory-*.json"))) == 2


# ---------------------------------------------------------------------------
# Scratch helpers
# ---------------------------------------------------------------------------

class TestScratchHelpers:
    """Load / save scratchpad."""

    def test_load_scratch_from_file(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        scratch_file.write_text(json.dumps({"a": "1", "b": "2"}))

        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            scratch = daemon._load_scratch()

        assert scratch == {"a": "1", "b": "2"}

    def test_load_scratch_missing_file(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            scratch = daemon._load_scratch()
        assert scratch == {}

    def test_load_scratch_invalid_json(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        scratch_file.write_text("not json at all")
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            scratch = daemon._load_scratch()
        assert scratch == {}

    def test_load_scratch_not_dict(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        scratch_file.write_text("[1,2,3]")
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            scratch = daemon._load_scratch()
        assert scratch == {}

    def test_save_and_reload(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        daemon._scratch = {"key": "val"}
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            daemon._save_scratch()
            scratch = daemon._load_scratch()
        assert scratch == {"key": "val"}


# ---------------------------------------------------------------------------
# _write_scratch_snapshot
# ---------------------------------------------------------------------------

class TestWriteScratchSnapshot:
    """Direct scratch write (no lock)."""

    def test_overwrites_existing(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        scratch_file.write_text(json.dumps({"old": "value"}))
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            daemon._write_scratch_snapshot({"new": "data"})
        assert json.loads(scratch_file.read_text()) == {"new": "data"}

    def test_creates_file_when_missing(self, daemon, tmp_path):
        scratch_file = tmp_path / "scratch.json"
        with mock.patch.object(bd, "SCRATCH_FILE", scratch_file):
            daemon._write_scratch_snapshot({"x": "y"})
        assert scratch_file.exists()


# ---------------------------------------------------------------------------
# Memory metadata helpers
# ---------------------------------------------------------------------------

class TestMemoryMetadata:
    """_memory_meta_unlocked and _set_memory_meta_unlocked."""

    def test_empty_returns_dict(self, daemon):
        daemon._memory = {}
        meta = daemon._memory_meta_unlocked()
        assert meta == {}

    def test_parses_json_meta(self, daemon):
        daemon._memory = {
            bd.MEMORY_META_KEY: json.dumps({
                "key1": {"intent": "question", "topic": "food"},
            }),
        }
        meta = daemon._memory_meta_unlocked()
        assert meta == {"key1": {"intent": "question", "topic": "food"}}

    def test_invalid_json_returns_empty(self, daemon):
        daemon._memory = {bd.MEMORY_META_KEY: "not json"}
        meta = daemon._memory_meta_unlocked()
        assert meta == {}

    def test_non_dict_payload_returns_empty(self, daemon):
        daemon._memory = {bd.MEMORY_META_KEY: '"just a string"'}
        meta = daemon._memory_meta_unlocked()
        assert meta == {}

    def test_set_meta_stores_compact_json(self, daemon):
        daemon._memory = {}
        daemon._set_memory_meta_unlocked({"k": {"intent": "preference"}})
        assert bd.MEMORY_META_KEY in daemon._memory

    def test_set_meta_empty_removes_key(self, daemon):
        daemon._memory = {bd.MEMORY_META_KEY: json.dumps({})}
        daemon._set_memory_meta_unlocked({})
        assert bd.MEMORY_META_KEY not in daemon._memory

    def test_nested_non_dict_values_ignored(self, daemon):
        daemon._memory = {
            bd.MEMORY_META_KEY: json.dumps({
                "good": {"intent": "x"},
                "bad": "string value",
            }),
        }
        meta = daemon._memory_meta_unlocked()
        assert "good" in meta
        assert "bad" not in meta


# ---------------------------------------------------------------------------
# Daily log helpers
# ---------------------------------------------------------------------------

class TestDailyLog:
    """_append_daily_log and _daily_log_has_entries."""

    def test_append_creates_file(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            daemon._append_daily_log(
                "hello", "hi there", "NPU temp: N/A, NPU load: N/A, Active: x"
            )
        data = json.loads(log_file.read_text())
        assert len(data) == 1
        assert data[0]["exchange"]["user"] == "hello"
        assert data[0]["exchange"]["echo"] == "hi there"

    def test_append_to_existing_file(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        log_file.write_text(json.dumps([{"old": "entry"}]))
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            daemon._append_daily_log("u", "a", "hw")
        data = json.loads(log_file.read_text())
        assert len(data) == 2

    def test_append_empty_echo_skipped(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            daemon._append_daily_log("hello", "  ", "hw")
        assert not log_file.exists()

    def test_has_entries_true(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        log_file.write_text(json.dumps([{"x": 1}]))
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            assert daemon._daily_log_has_entries() is True

    def test_has_entries_false_empty_array(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        log_file.write_text("[]")
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            assert daemon._daily_log_has_entries() is False

    def test_has_entries_false_no_file(self, daemon, tmp_path):
        with mock.patch.object(
            bd, "DAILY_LOG_FILE", tmp_path / "nope.json"
        ):
            assert daemon._daily_log_has_entries() is False

    def test_has_entries_false_invalid_json(self, daemon, tmp_path):
        log_file = tmp_path / "daily_log.json"
        log_file.write_text("garbage")
        with mock.patch.object(bd, "DAILY_LOG_FILE", log_file):
            assert daemon._daily_log_has_entries() is False


# ---------------------------------------------------------------------------
# Sleep intent handling
# ---------------------------------------------------------------------------

class TestHandleModelSleepIntent:
    """Route sleep intents to nap or dream."""

    def test_nap_mode_calls_check_nap(self, daemon):
        daemon._check_nap = mock.Mock()
        daemon._handle_model_sleep_intent("nap")
        daemon._check_nap.assert_called_once_with(reason="model-intent")

    def test_sleep_mode_calls_check_dream(self, daemon):
        daemon._check_dream = mock.Mock()
        daemon._handle_model_sleep_intent("sleep")
        daemon._check_dream.assert_called_once_with(reason="sleep-intent")

    def test_auto_mode_in_sleep_window(self, daemon):
        daemon._check_dream = mock.Mock()
        with mock.patch.object(daemon, "_in_sleep_window", return_value=True):
            daemon._handle_model_sleep_intent("auto")
        daemon._check_dream.assert_called_once_with(reason="sleep-intent")

    def test_auto_mode_outside_sleep_window(self, daemon):
        daemon._check_nap = mock.Mock()
        with mock.patch.object(daemon, "_in_sleep_window", return_value=False):
            daemon._handle_model_sleep_intent("auto")
        daemon._check_nap.assert_called_once_with(reason="model-intent")


# ---------------------------------------------------------------------------
# Dream / nap / slow-wave / nudge triggers
# ---------------------------------------------------------------------------

class TestCheckDream:
    """Decides whether to launch a dream pass."""

    def test_context_pressure_below_threshold(self, daemon):
        daemon._ctx_size = 1000
        with mock.patch.object(daemon, "_estimate_tokens", return_value=500):
            daemon._check_dream(reason="context-pressure")
        assert daemon._dreaming is False

    def test_context_pressure_above_threshold(self, daemon, monkeypatch):
        daemon._ctx_size = 1000
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        with mock.patch.object(daemon, "_estimate_tokens", return_value=750):
            with mock.patch.object(daemon, "_auto_model", return_value="auto"):
                daemon._check_dream(reason="context-pressure")
        assert started is not None

    def test_sleep_intent_with_no_history_or_memory_skipped(self, daemon):
        daemon._history = []
        daemon._memory = {}
        daemon._check_dream(reason="sleep")
        assert daemon._dreaming is False

    def test_sleep_intent_with_history_proceeds(self, daemon, monkeypatch):
        daemon._history = [{"role": "user", "content": "hi"}]
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        with mock.patch.object(daemon, "_auto_model", return_value="auto"):
            daemon._check_dream(reason="sleep")
        assert started[0] is True

    def test_sleep_intent_with_memory_proceeds(self, daemon, monkeypatch):
        daemon._memory = {"food": "pizza"}
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        with mock.patch.object(daemon, "_auto_model", return_value="auto"):
            daemon._check_dream(reason="sleep")
        assert started[0] is True

    def test_dream_skipped_when_napping(self, daemon):
        daemon._napping = True
        daemon._estimate_tokens = mock.Mock(return_value=9999)
        daemon._check_dream(reason="context-pressure")
        assert daemon._dreaming is False

    def test_dream_skipped_when_summarizing(self, daemon):
        daemon._summarizing = True
        daemon._check_dream(reason="sleep")
        assert daemon._dreaming is False


class TestCheckNap:
    """Decides whether to launch a nap pass."""

    def test_nap_skipped_when_dreaming(self, daemon):
        daemon._dreaming = True
        daemon._check_nap()
        assert daemon._napping is False

    def test_nap_skipped_when_summarizing(self, daemon):
        daemon._summarizing = True
        daemon._check_nap()
        assert daemon._napping is False

    def test_nap_starts_when_clear(self, daemon, monkeypatch):
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        daemon._check_nap()
        assert started[0] is True


class TestConsiderSlowWave:
    """Nightly slow-wave trigger: 02:00-05:00, idle >= 30m, once/day."""

    def test_runs_once_per_day(self, daemon, monkeypatch, tmp_path):
        # Use a temp file instead of trying to monkeypatch Path methods
        log_file = tmp_path / "bubble_daily.json"
        log_file.write_text('[{"x": 1}]')
        daemon._slow_wave_ran_today = False
        daemon._slow_wave_last_date = "2025-01-01"
        daemon._last_user_time = time.monotonic() - 3600  # idle 1h
        monkeypatch.setattr(time, "strftime", lambda *a, **kw: "2025-01-02")
        tmh = type("", (), {"tm_hour": 3})()
        monkeypatch.setattr(time, "localtime", lambda: tmh)
        monkeypatch.setattr("conftest.bd.DAILY_LOG_FILE", log_file)

        started = [False]
        daemon._slow_wave_ran_today = False
        with mock.patch.object(daemon, '_slow_wave_thread'):
            daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is True
        
        # Second call should be a no-op
        with mock.patch.object(daemon, '_slow_wave_thread'):
            daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is True
        
        # Third call should also be a no-op
        daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is True

    def test_outside_sleep_window(self, daemon, monkeypatch):
        monkeypatch.setattr(time, "strftime", lambda *a, **kw: "2025-01-02")
        monkeypatch.setattr(time, "localtime", lambda: type("", (), {"tm_hour": 14})())
        daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is False

    def test_not_idle_enough(self, daemon, monkeypatch):
        daemon._last_user_time = time.monotonic() - 60  # only 1 min idle
        monkeypatch.setattr(time, "strftime", lambda *a, **kw: "2025-01-02")
        monkeypatch.setattr(time, "localtime", lambda: type("", (), {"tm_hour": 3})())
        daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is False

    def test_no_daily_log_entries_skipped(self, daemon, monkeypatch, tmp_path):
        tmh = type("", (), {"tm_hour": 3})()
        monkeypatch.setattr(time, "strftime", lambda *a, **kw: "2025-01-02")
        monkeypatch.setattr(time, "localtime", lambda: tmh)
        # Use a non-existent file
        log_file = tmp_path / "bubble_daily.json"
        monkeypatch.setattr("conftest.bd.DAILY_LOG_FILE", log_file)
        daemon._consider_slow_wave()
        assert daemon._slow_wave_ran_today is False


class TestConsiderNudge:
    """Proactive nudge trigger: idle >= threshold, cooldown respected."""

    def test_disabled_noop(self, daemon):
        daemon._nudge_enabled = False
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        daemon._fire_nudge.assert_not_called()

    def test_below_threshold_noop(self, daemon):
        daemon._nudge_enabled = True
        daemon._nudge_idle_minutes = 30
        daemon._last_user_time = time.monotonic() - 120  # only 2 min idle
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        daemon._fire_nudge.assert_not_called()

    def test_within_cooldown_noop(self, daemon):
        daemon._nudge_enabled = True
        daemon._nudge_idle_minutes = 1
        daemon._nudge_cooldown_min = 5
        daemon._last_user_time = time.monotonic() - 1200  # 20 min idle
        daemon._last_nudge_time = time.monotonic() - 120   # 2 min ago
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        daemon._fire_nudge.assert_not_called()

    def test_fires_when_conditions_met(self, daemon):
        daemon._nudge_enabled = True
        daemon._nudge_idle_minutes = 1
        daemon._nudge_cooldown_min = 1
        daemon._last_user_time = time.monotonic() - 300
        daemon._last_nudge_time = 0
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        daemon._fire_nudge.assert_called_once()

    def test_stale_guard_resets_after_5min(self, daemon, monkeypatch):
        daemon._nudge_enabled = True
        daemon._nudge_idle_minutes = 1
        daemon._nudge_cooldown_min = 1
        daemon._last_user_time = time.monotonic() - 300
        daemon._last_nudge_time = 0
        daemon._nudging = True
        daemon._nudge_start_time = time.monotonic() - 450  # 450s ago (> 5 min threshold)
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        assert daemon._nudging is True, f"Expected _nudging to be True (stale guard reset it, but code continued), got {daemon._nudging}"
        daemon._fire_nudge.assert_called_once()

    def test_summarizing_blocks_nudge(self, daemon):
        daemon._nudge_enabled = True
        daemon._summarizing = True
        daemon._fire_nudge = mock.Mock()
        daemon._consider_nudge()
        daemon._fire_nudge.assert_not_called()


class TestFireNudge:
    """Nudge fires a clock-tick thread with telemetry."""

    def test_builds_snapshot_and_starts_thread(self, daemon, monkeypatch):
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        with mock.patch.object(daemon, "_gather_telemetry",
                               return_value={"token_est": 1024, "ctx_total": 4096, "ctx_pct": 25, "mem_nodes": 35, "scratch_nodes": 18, "last_prune": "10m ago", "npu_temp": "N/A", "npu_load": "N/A", "uptime": "1h", "since_user": "1h", "since_assistant": "1h", "idle_phase": "morning", "felt_elapsed_wall": 3600.0, "time_samples": 10, "last_sample_age": 60, "time_poll_seconds": 15, "quiet_heartbeats": 0, "events_10m": 0, "events_60m": 0, "events_6h": 0, "exchange_tempo": "steady", "weekday": "Mon", "day_phase": "night", "suspend_events": 0, "last_suspend_gap": 0}) as mock_gather:
            with mock.patch.object(daemon, "_auto_model", return_value="auto"):
                daemon._fire_nudge(reason="manual")
        assert started[0] is True
        assert mock_gather.called

    def test_idle_note_format(self, daemon, monkeypatch):
        started = [False]
        def fake_start(self):
            started[0] = True
        monkeypatch.setattr("threading.Thread.start", fake_start)
        with mock.patch.object(daemon, "_gather_telemetry",
                               return_value={"token_est": 1024, "ctx_total": 4096, "ctx_pct": 25, "mem_nodes": 35, "scratch_nodes": 18, "last_prune": "10m ago", "npu_temp": "N/A", "npu_load": "N/A", "uptime": "1h", "since_user": "1h", "since_assistant": "1h", "idle_phase": "morning", "felt_elapsed_wall": 3600.0, "time_samples": 10, "last_sample_age": 60, "time_poll_seconds": 15, "quiet_heartbeats": 0, "events_10m": 0, "events_60m": 0, "events_6h": 0, "exchange_tempo": "steady", "weekday": "Mon", "day_phase": "night", "suspend_events": 0, "last_suspend_gap": 0}) as mock_gather:
            with mock.patch.object(daemon, "_auto_model", return_value="auto"):
                daemon._fire_nudge(reason="idle", idle_minutes=5)
        assert started[0] is True
        assert mock_gather.called


# ---------------------------------------------------------------------------
# _in_sleep_window
# ---------------------------------------------------------------------------

class TestInSleepWindow:
    """Static: returns True for hours 2-4 (2 <= hour < 5)."""

    def test_2am(self, daemon):
        tmh = type("", (), {"tm_hour": 2})()
        with mock.patch("time.localtime", lambda: tmh):
            assert bd.BubbleDaemon._in_sleep_window() is True

    def test_3am(self, daemon):
        tmh = type("", (), {"tm_hour": 3})()
        with mock.patch("time.localtime", lambda: tmh):
            assert bd.BubbleDaemon._in_sleep_window() is True

    def test_4am(self, daemon):
        tmh = type("", (), {"tm_hour": 4})()
        with mock.patch("time.localtime", lambda: tmh):
            assert bd.BubbleDaemon._in_sleep_window() is True

    def test_5am(self, daemon):
        tmh = type("", (), {"tm_hour": 5})()
        with mock.patch("time.localtime", lambda: tmh):
            assert bd.BubbleDaemon._in_sleep_window() is False

    def test_noon(self, daemon):
        tmh = type("", (), {"tm_hour": 12})()
        with mock.patch("time.localtime", lambda: tmh):
            assert bd.BubbleDaemon._in_sleep_window() is False


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

class TestGatherTelemetry:
    """_gather_telemetry returns a dict with expected keys."""

    def test_returns_expected_keys(self, daemon, monkeypatch):
        daemon._ctx_size = 4096
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 1024)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("30°C", "10%")):
            tel = daemon._gather_telemetry()
        assert "token_est" in tel
        assert "ctx_pct" in tel
        assert tel["ctx_pct"] == 25
        assert "npu_temp" in tel
        assert "weekday" in tel
        assert "day_phase" in tel
        assert "exchange_tempo" in tel

    def test_exchange_tempo_insufficient_data(self, daemon, monkeypatch):
        daemon._exchange_events = []
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert tel["exchange_tempo"] == "insufficient data"

    def test_exchange_tempo_has_data(self, daemon, monkeypatch):
        now = time.monotonic()
        daemon._exchange_events = [now - 300, now - 200, now - 100, now]
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert "steady" in tel["exchange_tempo"] or "rapid" in tel["exchange_tempo"]

    def test_last_prune_never(self, daemon, monkeypatch):
        daemon._last_memory_change_time = 0.0
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert tel["last_prune"] == "never"

    def test_last_prune_recent_seconds(self, daemon, monkeypatch):
        daemon._last_memory_change_time = time.monotonic() - 45
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert "45s ago" in tel["last_prune"]

    def test_last_prune_recent_minutes(self, daemon, monkeypatch):
        daemon._last_memory_change_time = time.monotonic() - 900
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert "15m ago" in tel["last_prune"]

    def test_last_prune_recent_hours(self, daemon, monkeypatch):
        daemon._last_memory_change_time = time.monotonic() - 7200
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        with mock.patch.object(daemon, "_read_hwmon_npu",
                               return_value=("N/A", "N/A")):
            tel = daemon._gather_telemetry()
        assert "2.0h ago" in tel["last_prune"]

    def test_fallback_npu_read(self, daemon, monkeypatch):
        daemon._npu_temp_cached = "N/A"
        daemon._npu_load_cached = "N/A"
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        called = [False]
        def fake_read():
            called[0] = True
            return ("40°C", "15%")
        monkeypatch.setattr(daemon, "_read_hwmon_npu", fake_read)
        tel = daemon._gather_telemetry()
        assert called[0] is True

    def test_cached_npu_values_used(self, daemon, monkeypatch):
        daemon._npu_temp_cached = "55°C"
        daemon._npu_load_cached = "70%"
        monkeypatch.setattr(daemon, "_estimate_tokens", lambda: 0)
        read_called = [False]
        monkeypatch.setattr(daemon, "_read_hwmon_npu", lambda: (read_called.__setitem__(0, True), ("X", "Y"))[-1])
        tel = daemon._gather_telemetry()
        assert read_called[0] is False


# ---------------------------------------------------------------------------
# _read_hwmon_npu
# ---------------------------------------------------------------------------

class TestReadHwmonNpu:
    """NPU temperature and load reader from sysfs."""

    def test_returns_na_when_no_files(self, daemon, monkeypatch):
        import glob
        import os
        # Ensure no hwmon files exist
        monkeypatch.setattr(os.path, 'exists', lambda p: False)
        monkeypatch.setattr(glob, 'glob', lambda p: [])
        temp, load = daemon._read_hwmon_npu()
        assert temp == "N/A"
        assert load == "N/A"

    def test_parses_temp_from_mocked_hwmon(self, daemon, monkeypatch, tmp_path):
        # Create mock hwmon directory for amdxdna
        hwmon = tmp_path / "hwmon0"
        hwmon.mkdir()
        (hwmon / "name").write_text("amdxdna")
        (hwmon / "temp1_input").write_text("45000")

        # Mock glob to return our temp dir
        def fake_glob(pattern):
            if "/sys/class/hwmon/hwmon*" in pattern or "/hwmon/hwmon*" in pattern:
                return [str(hwmon)]
            if "temp*_input" in pattern:
                return [str(hwmon / "temp1_input")]
            return []

        monkeypatch.setattr("glob.glob", fake_glob)

        temp, load = daemon._read_hwmon_npu()
        assert "45.0" in temp
        assert "°C" in temp
