"""
Tests for the D-Bus entry points on BubbleDaemon.

These methods form the public surface the GNOME extension calls over D-Bus.
No real D-Bus connection is needed — we instantiate the daemon directly
and call the methods (they're plain Python methods that pydbus happens to
expose).
"""

import json

import pytest
from conftest import bd


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_returns_pong(self, daemon):
        assert daemon.Ping() == "pong"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_set_and_get(self, daemon, tmp_path):
        daemon.SetSystemPrompt("you are helpful")
        assert daemon.GetSystemPrompt() == "you are helpful"

    def test_empty_string_clears_prompt(self, daemon, tmp_path):
        daemon.SetSystemPrompt("initial prompt")
        assert daemon.GetSystemPrompt() == "initial prompt"
        daemon.SetSystemPrompt("")
        assert daemon.GetSystemPrompt() == ""

    def test_long_prompt(self, daemon, tmp_path):
        long = "x" * 10000
        daemon.SetSystemPrompt(long)
        assert daemon.GetSystemPrompt() == long

    def test_prompt_survives_to_disk(self, daemon, tmp_path):
        daemon.SetSystemPrompt("persisted prompt")
        # The file should contain the prompt text
        text = (tmp_path / "system_prompt.txt").read_text()
        assert text == "persisted prompt"

    def test_prompt_loaded_from_disk_on_init(self, daemon, tmp_path, monkeypatch):
        # Pre-create the file with a prompt, then create a new daemon.
        monkeypatch.setenv("BUBBLE_CTX", "8192")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(bd, "MEMORY_FILE", tmp_path / "memory.json")
        monkeypatch.setattr(bd, "MEMORY_BACKUP_DIR", tmp_path / "memory-backups")
        monkeypatch.setattr(bd, "SCRATCH_FILE", tmp_path / "scratch.json")
        monkeypatch.setattr(bd, "SYSTEM_PROMPT_FILE", tmp_path / "system_prompt.txt")
        monkeypatch.setattr(bd, "DAILY_LOG_FILE", tmp_path / "bubble_daily.json")
        monkeypatch.setattr(bd.GLib, "idle_add", lambda *a, **k: None)

        (tmp_path / "system_prompt.txt").write_text("pre-loaded prompt")
        d = bd.BubbleDaemon()
        assert d.GetSystemPrompt() == "pre-loaded prompt"


# ---------------------------------------------------------------------------
# Memory CRUD
# ---------------------------------------------------------------------------

class TestMemory:
    def test_write_and_read(self, daemon):
        daemon.WriteMemory("color", "blue")
        assert daemon.ReadMemory("color") == "blue"

    def test_write_overwrites(self, daemon):
        daemon.WriteMemory("color", "blue")
        daemon.WriteMemory("color", "red")
        assert daemon.ReadMemory("color") == "red"

    def test_write_empty_value_deletes(self, daemon):
        daemon.WriteMemory("key", "value")
        assert daemon.ReadMemory("key") == "value"
        daemon.WriteMemory("key", "")
        assert daemon.ReadMemory("key") == ""

    def test_read_missing_key_returns_empty(self, daemon):
        assert daemon.ReadMemory("nonexistent") == ""

    def test_get_memory_returns_json(self, daemon):
        daemon.WriteMemory("a", "1")
        daemon.WriteMemory("b", "2")
        raw = daemon.GetMemory()
        data = json.loads(raw)
        assert data["a"] == "1"
        assert data["b"] == "2"

    def test_get_memory_empty(self, daemon):
        raw = daemon.GetMemory()
        data = json.loads(raw)
        assert isinstance(data, dict)

    def test_clear_memory(self, daemon):
        daemon.WriteMemory("key1", "val1")
        daemon.WriteMemory("key2", "val2")
        daemon.ClearMemory()
        assert daemon.ReadMemory("key1") == ""
        assert daemon.ReadMemory("key2") == ""
        data = json.loads(daemon.GetMemory())
        # After clear, only internal keys should remain (meta, self-prompt, etc.)
        assert not any(k for k in data if k not in {"_self_prompt", bd.PENDING_SELF_PROMPT_KEY, bd.PENDING_SELF_PROMPT_APPROVED_KEY, bd.MEMORY_META_KEY, ""}) or \
               all(k.startswith("_") for k in data if k)

    def test_memory_survives_to_disk(self, daemon, tmp_path):
        daemon.WriteMemory("test_key", "test_value")
        raw = (tmp_path / "memory.json").read_text()
        data = json.loads(raw)
        assert data["test_key"] == "test_value"

    def test_memory_meta_key_not_user_writable(self, daemon):
        """User shouldn't accidentally overwrite the internal meta key."""
        daemon.WriteMemory("_internal", "should work but is internal")
        # _internal keys ARE allowed via D-Bus; they're just not shown
        # in the memory protocol (remember/forget).
        # The protocol parser rejects _ keys, but D-Bus WriteMemory allows them.
        assert daemon.ReadMemory("_internal") == "should work but is internal"


# ---------------------------------------------------------------------------
# Self-prompt proposal
# ---------------------------------------------------------------------------

class TestSelfPromptProposal:
    def test_get_no_proposal(self, daemon):
        proposal, approved = daemon.GetSelfPromptProposal()
        assert proposal == ""
        assert approved is False

    def test_set_proposal_via_memory_then_get(self, daemon):
        # Proposals are set internally (via memory commands or D-Bus WriteMemory).
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "I am evolving")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        proposal, approved = daemon.GetSelfPromptProposal()
        assert proposal == "I am evolving"
        assert approved is False

    def test_approve_proposal(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "new self")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        daemon.SetSelfPromptProposalApproval(True)
        proposal, approved = daemon.GetSelfPromptProposal()
        assert approved is True

    def test_disapprove_proposal(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "new self")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "true")
        daemon.SetSelfPromptProposalApproval(False)
        proposal, approved = daemon.GetSelfPromptProposal()
        assert approved is False

    def test_set_approval_without_pending_noop(self, daemon):
        # Should not raise or change state.
        daemon.SetSelfPromptProposalApproval(True)
        proposal, approved = daemon.GetSelfPromptProposal()
        assert proposal == ""

    def test_clear_proposal(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "pending")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        daemon.ClearSelfPromptProposal()
        proposal, approved = daemon.GetSelfPromptProposal()
        assert proposal == ""

    def test_clear_memory_also_clears_proposal(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "pending")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        daemon.ClearMemory()
        proposal, approved = daemon.GetSelfPromptProposal()
        assert proposal == ""

    def test_clear_self_prompt_clears_only_proposal(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "pending")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        daemon.WriteMemory("other_key", "should remain")
        daemon.ClearSelfPromptProposal()
        assert daemon.ReadMemory("other_key") == "should remain"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TestHistory:
    def test_get_empty_history(self, daemon):
        raw = daemon.GetHistory()
        assert json.loads(raw) == []

    def test_clear_history(self, daemon):
        # History is populated by SendMessage, which requires HTTP.
        # Instead, manually add to _history and verify ClearHistory.
        daemon._history = [{"role": "user", "content": "hello"}]
        daemon.ClearHistory()
        assert json.loads(daemon.GetHistory()) == []

    def test_get_history_json(self, daemon):
        daemon._history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello!"},
        ]
        raw = daemon.GetHistory()
        data = json.loads(raw)
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[1]["content"] == "hello!"

    def test_clear_history_preserves_memory(self, daemon):
        daemon.WriteMemory("fact", "true")
        daemon._history = [{"role": "user", "content": "msg"}]
        daemon.ClearHistory()
        assert daemon.ReadMemory("fact") == "true"
        assert json.loads(daemon.GetHistory()) == []

    def test_get_history_unicode(self, daemon):
        daemon._history = [{"role": "user", "content": "こんにちは"}]
        raw = daemon.GetHistory()
        data = json.loads(raw)
        assert data[0]["content"] == "こんにちは"

    def test_history_persists_to_disk(self, daemon, tmp_path):
        daemon._history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        daemon._save_history()

        assert json.loads((tmp_path / "history.json").read_text()) == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert daemon._load_history() == daemon._history

    def test_clear_history_persists_empty(self, daemon, tmp_path):
        daemon._history = [{"role": "user", "content": "hello"}]
        daemon._save_history()

        daemon.ClearHistory()

        assert (tmp_path / "history.json").read_text() == "[]"
        assert daemon._load_history() == []

    def test_load_history_filters_non_message_entries(self, daemon, tmp_path):
        (tmp_path / "history.json").write_text(json.dumps([
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "ignore"},
            {"role": "assistant", "content": "hello"},
        ]))

        assert daemon._load_history() == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_load_history_corrupt_file(self, daemon, tmp_path):
        (tmp_path / "history.json").write_text("not json")
        assert daemon._load_history() == []


# ---------------------------------------------------------------------------
# Nudge config
# ---------------------------------------------------------------------------

class TestNudgeConfig:
    def test_set_config(self, daemon):
        daemon.SetNudgeConfig(False, 60)
        with daemon._lock:
            assert daemon._nudge_enabled is False
            assert daemon._nudge_idle_minutes == 60

    def test_idle_minutes_clamped_min(self, daemon):
        daemon.SetNudgeConfig(True, 0)
        with daemon._lock:
            assert daemon._nudge_idle_minutes >= 1

    def test_idle_minutes_clamped_max(self, daemon):
        daemon.SetNudgeConfig(True, 1000)
        with daemon._lock:
            assert daemon._nudge_idle_minutes <= 480

    def test_trigger_nudge_does_not_crash(self, daemon, monkeypatch):
        """TriggerNudge launches a thread; verify it doesn't raise."""
        fired = []
        def fake_fire(reason="idle", idle_minutes=0):
            fired.append(reason)
        monkeypatch.setattr(daemon, "_fire_nudge", fake_fire)
        daemon.TriggerNudge()
        assert fired == ["manual"]
