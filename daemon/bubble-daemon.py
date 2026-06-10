#!/usr/bin/env python3
"""
Bubble Daemon
=============
D-Bus service (com.bubble.Daemon) that:
  - Maintains a rolling conversation history
  - Streams completions from a FastFlowLM server over SSE
  - Emits StreamChunk / StreamDone / StreamError signals back to the extension
"""

import os
import re
import sys
import json
import time
import math
import hashlib
import shutil
import threading
import logging
from collections import deque
from pathlib import Path

import requests
from requests.exceptions import ChunkedEncodingError
from gi.repository import GLib
from pydbus import SessionBus
from pydbus.generic import signal

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("bubble")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _get_port() -> int:
    env = os.environ.get("FLM_SERVE_PORT", "")
    if env.isdigit():
        p = int(env)
        if 1 <= p <= 65535:
            return p
    return 52625  # FastFlowLM default


# Keep at most this many user+assistant turns before trimming the oldest.
MAX_HISTORY_TURNS = 50

# Summarize when live history exceeds this many turns (pairs).
# The oldest (SUMMARIZE_AT_TURNS - SUMMARIZE_KEEP_TURNS) pairs are compressed.
SUMMARIZE_AT_TURNS   = 40  # trigger when history ≥ this many pairs
SUMMARIZE_KEEP_TURNS = 20  # always keep this many recent pairs intact
MAX_MEMORY_ITEMS_IN_PROMPT = 24
MEMORY_META_KEY = "_memory_meta"
DEFAULT_EMBED_MODEL = "embed-gemma:300m"
DEFAULT_EMBED_TIMEOUT = 3.0
DEFAULT_EMBED_MAX_TEXT_CHARS = 1600

TAG_FIELDS = {"intent", "topic", "sentiment", "user", "user_identity"}

# JSON file that persists named facts across sessions.
MEMORY_FILE  = Path.home() / ".local" / "share" / "bubble" / "memory.json"
MEMORY_BACKUP_DIR = Path.home() / ".local" / "share" / "bubble" / "memory-backups"
# JSON file that persists working notes (temporary, model-managed scratchpad).
SCRATCH_FILE = Path.home() / ".local" / "share" / "bubble" / "scratch.json"
# Persistent user-facing system prompt set over D-Bus.
SYSTEM_PROMPT_FILE = Path.home() / ".local" / "share" / "bubble" / "system_prompt.txt"

PENDING_SELF_PROMPT_KEY = "_self_prompt_proposal"
PENDING_SELF_PROMPT_APPROVED_KEY = "_self_prompt_proposal_approved"

def _runtime_dir() -> Path:
    env = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if env:
        return Path(env)
    return Path(f"/run/user/{os.getuid()}")

# Live daily exchange log consumed by the slow-wave sleep cycle.
DAILY_LOG_FILE = _runtime_dir() / "bubble_daily.json"


def _ensure_session_bus_address() -> None:
    """Point libdbus clients at the per-user session bus if env is missing."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip():
        return
    bus_socket = _runtime_dir() / "bus"
    if bus_socket.exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_socket}"
        log.info("DBUS_SESSION_BUS_ADDRESS was unset; defaulted to %s", os.environ["DBUS_SESSION_BUS_ADDRESS"])

# Estimated context window size (tokens).  Override with BUBBLE_CTX env var.
# Used only for the context-pressure dream trigger; not sent to the model.
def _get_ctx_size() -> int:
    env = os.environ.get("BUBBLE_CTX", "")
    if env.isdigit():
        v = int(env)
        if v > 0:
            return v
    return 8192


def _get_memory_backup_limit() -> int:
    env = os.environ.get("BUBBLE_MEMORY_BACKUP_LIMIT", "")
    if env.isdigit():
        v = int(env)
        if 1 <= v <= 500:
            return v
    return 50

# ---------------------------------------------------------------------------
# D-Bus XML introspection
# ---------------------------------------------------------------------------
DBUS_XML = """<node>
  <interface name='com.bubble.Daemon'>

    <method name='SendMessage'>
      <arg type='s' name='message'      direction='in'/>
      <arg type='s' name='token'        direction='in'/>
      <arg type='s' name='context_json' direction='in'/>
    </method>

    <method name='SetSystemPrompt'>
      <arg type='s' name='prompt' direction='in'/>
    </method>

    <method name='GetSystemPrompt'>
      <arg type='s' name='prompt' direction='out'/>
    </method>

    <method name='ClearHistory'/>

    <method name='GetHistory'>
      <arg type='s' name='history_json' direction='out'/>
    </method>

    <method name='WriteMemory'>
      <arg type='s' name='key'   direction='in'/>
      <arg type='s' name='value' direction='in'/>
    </method>

    <method name='ReadMemory'>
      <arg type='s' name='key'   direction='in'/>
      <arg type='s' name='value' direction='out'/>
    </method>

    <method name='GetMemory'>
      <arg type='s' name='memory_json' direction='out'/>
    </method>

    <method name='ClearMemory'/>

        <method name='GetSelfPromptProposal'>
            <arg type='s' name='proposal' direction='out'/>
            <arg type='b' name='approved' direction='out'/>
        </method>

        <method name='SetSelfPromptProposalApproval'>
            <arg type='b' name='approved' direction='in'/>
        </method>

        <method name='ClearSelfPromptProposal'/>

    <method name='SetNudgeConfig'>
      <arg type='b' name='enabled'      direction='in'/>
      <arg type='i' name='idle_minutes' direction='in'/>
    </method>

    <method name='TriggerNudge'/>

    <method name='Ping'>
      <arg type='s' name='pong' direction='out'/>
    </method>

    <signal name='StreamChunk'>
      <arg type='s' name='token'/>
      <arg type='s' name='text'/>
    </signal>

    <signal name='StreamThink'>
      <arg type='s' name='token'/>
      <arg type='s' name='text'/>
    </signal>

    <signal name='StreamDone'>
      <arg type='s' name='token'/>
    </signal>

    <signal name='StreamEdit'>
      <arg type='s' name='token'/>
      <arg type='s' name='text'/>
    </signal>

    <signal name='NudgeStart'>
      <arg type='s' name='token'/>
    </signal>

        <signal name='StatusChanged'>
            <arg type='s' name='state'/>
            <arg type='s' name='detail'/>
        </signal>

        <signal name='SelfPromptProposalChanged'>
            <arg type='b' name='has_pending'/>
            <arg type='b' name='approved'/>
        </signal>

    <signal name='StreamError'>
      <arg type='s' name='token'/>
      <arg type='s' name='error_msg'/>
    </signal>

  </interface>
</node>"""


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------
class BubbleDaemon:
    dbus = DBUS_XML

    StreamChunk = signal()
    StreamThink = signal()
    StreamDone  = signal()
    StreamEdit  = signal()
    StreamError = signal()
    NudgeStart  = signal()
    StatusChanged = signal()
    SelfPromptProposalChanged = signal()

    # ------------------------------------------------------------------
    def __init__(self) -> None:
        self._history: list[dict] = []
        self._system_prompt: str  = os.environ.get("BUBBLE_SYSTEM_PROMPT", "")
        if not self._system_prompt:
            self._system_prompt = self._load_system_prompt()
        self._lock    = threading.Lock()
        self._port    = _get_port()
        self._model   = os.environ.get("BUBBLE_MODEL", "")
        self._memory: dict[str, str] = self._load_memory()
        self._scratch: dict[str, str] = self._load_scratch()
        self._summarizing = False  # prevents concurrent summarization runs
        self._dreaming    = False  # prevents concurrent dream runs
        self._napping     = False  # prevents concurrent nap runs
        self._ctx_size    = _get_ctx_size()
        self._daemon_start_mono = time.monotonic()
        self._daemon_start_wall = time.time()
        self._embed_mode = os.environ.get("BUBBLE_EMBEDDINGS_ENABLED", "auto").strip().lower()
        if self._embed_mode not in {"auto", "on", "off"}:
            self._embed_mode = "auto"
        self._embed_model = os.environ.get("BUBBLE_EMBED_MODEL", DEFAULT_EMBED_MODEL).strip() or DEFAULT_EMBED_MODEL
        self._embed_timeout = self._get_float_env("BUBBLE_EMBED_TIMEOUT", DEFAULT_EMBED_TIMEOUT, 0.25, 30.0)
        self._embed_max_text_chars = self._get_int_env(
            "BUBBLE_EMBED_MAX_TEXT_CHARS",
            DEFAULT_EMBED_MAX_TEXT_CHARS,
            128,
            4096,
        )
        self._embed_enabled_checked = False
        self._embed_enabled_runtime = False
        self._embed_cache: dict[str, list[float]] = {}
        self._embed_lock = threading.Lock()
        # Monotonic timestamp of the last event that changed _memory (remember,
        # forget, dream, summarize).  Used in telemetry reporting.
        self._last_memory_change_time: float = 0.0

        # Nudge state
        self._nudge_enabled      = True
        self._nudge_idle_minutes = 30
        self._nudge_cooldown_min = 60   # minimum gap between any two nudges
        self._last_user_time     = time.monotonic()
        self._last_assistant_time = 0.0
        self._last_nudge_time    = 0.0  # epoch 0 = never
        self._nudging            = False
        self._nudge_start_time   = 0.0  # monotonic when _nudging was set True
        self._idle_phase_start_time = self._last_user_time
        self._time_poll_seconds = self._get_int_env(
            "BUBBLE_TIME_POLL_SECONDS",
            15,
            5,
            300,
        )
        self._time_samples = 0
        self._last_time_sample = 0.0
        self._last_time_sample_wall = time.time()
        self._felt_elapsed_wall = 0.0
        self._suspend_gap_events = 0
        self._last_suspend_gap = 0.0
        self._exchange_events = deque(maxlen=512)
        self._npu_temp_cached = "N/A"
        self._npu_load_cached = "N/A"

        # Slow-wave state (nightly, runs at most once per local calendar day).
        self._slow_wave_ran_today: bool = False
        self._slow_wave_last_date: str = ""

        # Start the background watcher
        threading.Thread(
            target=self._watcher_thread,
            daemon=True,
            name="bubble-watcher",
        ).start()

        # Subscribe to system sleep signal so we can dream before suspend.
        self._subscribe_sleep_signal()
        log.info(
            "Bubble daemon ready  port=%d  model=%s  embeddings=%s(%s)  time_poll=%ss",
            self._port,
            self._model or "(auto-detect)",
            self._embed_mode,
            self._embed_model,
            self._time_poll_seconds,
        )
        if self._system_prompt:
            log.info("System prompt loaded from environment (%d chars)",
                     len(self._system_prompt))

    @staticmethod
    def _get_int_env(key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.environ.get(key, "").strip()
        if raw.isdigit():
            value = int(raw)
            if min_value <= value <= max_value:
                return value
        return default

    @staticmethod
    def _get_float_env(key: str, default: float, min_value: float, max_value: float) -> float:
        raw = os.environ.get(key, "").strip()
        try:
            value = float(raw)
            if min_value <= value <= max_value:
                return value
        except Exception:
            pass
        return default

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        secs = max(0, int(seconds))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        if secs < 86400:
            h = secs // 3600
            m = (secs % 3600) // 60
            return f"{h}h {m}m"
        d = secs // 86400
        h = (secs % 86400) // 3600
        return f"{d}d {h}h"

    def _sample_temporal_state(self) -> None:
        """Periodic low-cost sampler for time passage and hardware state."""
        temp, load = self._read_hwmon_npu()
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            wall_delta = max(0.0, now_wall - self._last_time_sample_wall)
            mono_delta = max(0.0, now_mono - self._last_time_sample)
            # Clamp pathological jumps but still preserve suspend/resume drift.
            self._felt_elapsed_wall += min(wall_delta, 3600.0)
            drift = wall_delta - mono_delta
            if self._last_time_sample > 0.0 and drift >= 30.0:
                self._suspend_gap_events += 1
                self._last_suspend_gap = drift
            self._last_time_sample_wall = now_wall
            self._last_time_sample = now_mono
            self._time_samples += 1
            self._npu_temp_cached = temp
            self._npu_load_cached = load

    @staticmethod
    def _day_phase(hour: int) -> str:
        if 5 <= hour < 9:
            return "early morning"
        if 9 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    @staticmethod
    def _tempo_label(avg_gap_secs: float) -> str:
        if avg_gap_secs < 90:
            return "rapid"
        if avg_gap_secs < 300:
            return "steady"
        if avg_gap_secs < 1200:
            return "reflective"
        return "sporadic"

    def _record_exchange_event_unlocked(self, when_mono: float | None = None) -> None:
        self._exchange_events.append(when_mono if when_mono is not None else time.monotonic())

    # ------------------------------------------------------------------
    # D-Bus methods
    # ------------------------------------------------------------------
    def Ping(self) -> str:
        return "pong"

    def _set_status(self, state: str, detail: str = "") -> None:
        """Emit a lightweight global status update for the extension UI."""
        GLib.idle_add(self.StatusChanged, state, detail)

    def _proposal_state_unlocked(self) -> tuple[bool, bool]:
        pending = bool(self._memory.get(PENDING_SELF_PROMPT_KEY, "").strip())
        approved = self._memory.get(PENDING_SELF_PROMPT_APPROVED_KEY, "") == "true"
        return pending, approved

    def _emit_proposal_state_unlocked(self) -> None:
        pending, approved = self._proposal_state_unlocked()
        GLib.idle_add(self.SelfPromptProposalChanged, pending, approved)

    def _emit_proposal_state(self) -> None:
        with self._lock:
            self._emit_proposal_state_unlocked()

    def _set_pending_self_prompt_unlocked(self, prompt: str) -> bool:
        prompt = prompt.strip()
        if not prompt:
            return False
        current = self._memory.get(PENDING_SELF_PROMPT_KEY, "")
        if current == prompt and self._memory.get(PENDING_SELF_PROMPT_APPROVED_KEY, "") == "false":
            return False
        self._memory[PENDING_SELF_PROMPT_KEY] = prompt
        self._memory[PENDING_SELF_PROMPT_APPROVED_KEY] = "false"
        return True

    def _clear_pending_self_prompt_unlocked(self) -> bool:
        changed = False
        if self._memory.pop(PENDING_SELF_PROMPT_KEY, None) is not None:
            changed = True
        if self._memory.pop(PENDING_SELF_PROMPT_APPROVED_KEY, None) is not None:
            changed = True
        return changed

    def SetSystemPrompt(self, prompt: str) -> None:
        """Replace the active system prompt. Empty string disables it."""
        with self._lock:
            self._system_prompt = prompt
        self._save_system_prompt(prompt)
        log.info("System prompt updated (%d chars)", len(prompt))

    def GetSystemPrompt(self) -> str:
        with self._lock:
            return self._system_prompt

    def SetNudgeConfig(self, enabled: bool, idle_minutes: int) -> None:
        """Called by the extension to push gsettings nudge config to the daemon."""
        with self._lock:
            self._nudge_enabled      = enabled
            self._nudge_idle_minutes = max(1, min(480, idle_minutes))
        log.info(
            "Nudge config updated: enabled=%s  idle=%d min",
            enabled, self._nudge_idle_minutes,
        )

    def TriggerNudge(self) -> None:
        """Manually fire a nudge immediately (for testing)."""
        log.info("TriggerNudge called manually")
        self._fire_nudge(reason="manual")

    def WriteMemory(self, key: str, value: str) -> None:
        """Set a memory entry. Pass an empty value to delete the key."""
        with self._lock:
            if value:
                self._memory[key] = value
            else:
                self._memory.pop(key, None)
        self._save_memory()
        log.info("Memory write (D-Bus): %r = %r", key, value)

    def ReadMemory(self, key: str) -> str:
        with self._lock:
            return self._memory.get(key, "")

    def GetMemory(self) -> str:
        with self._lock:
            return json.dumps(self._memory)

    def ClearMemory(self) -> None:
        with self._lock:
            self._memory.clear()
        self._save_memory()
        self._emit_proposal_state()
        log.info("Memory cleared (D-Bus)")

    def GetSelfPromptProposal(self) -> tuple[str, bool]:
        with self._lock:
            proposal = self._memory.get(PENDING_SELF_PROMPT_KEY, "")
            approved = self._memory.get(PENDING_SELF_PROMPT_APPROVED_KEY, "") == "true"
        return proposal, approved

    def SetSelfPromptProposalApproval(self, approved: bool) -> None:
        with self._lock:
            if PENDING_SELF_PROMPT_KEY not in self._memory:
                return
            self._memory[PENDING_SELF_PROMPT_APPROVED_KEY] = "true" if approved else "false"
            self._last_memory_change_time = time.monotonic()
        self._save_memory()
        self._emit_proposal_state()
        log.info("Self-prompt proposal approval set: %s", approved)

    def ClearSelfPromptProposal(self) -> None:
        changed = False
        with self._lock:
            changed = self._clear_pending_self_prompt_unlocked()
            if changed:
                self._last_memory_change_time = time.monotonic()
        if changed:
            self._save_memory()
            self._emit_proposal_state()
            log.info("Self-prompt proposal cleared")

    def _load_memory(self) -> dict:
        """Load persisted memory from disk; returns empty dict on first run."""
        try:
            if MEMORY_FILE.exists():
                data = json.loads(MEMORY_FILE.read_text())
                if isinstance(data, dict):
                    log.info("Memory loaded: %d entries", len(data))
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            log.warning("Could not load memory file: %s", exc)
        return {}

    def _save_memory(self) -> None:
        """Atomically write the current memory dict to disk."""
        with self._lock:
            snapshot = dict(self._memory)
        new_payload = json.dumps(snapshot, indent=2)
        try:
            if MEMORY_FILE.exists():
                self._backup_existing_memory_file(new_payload)
            MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(new_payload)
            tmp.replace(MEMORY_FILE)
        except OSError as exc:
            log.error("Failed to save memory: %s", exc)

    def _backup_existing_memory_file(self, new_payload: str) -> None:
        """Save a timestamped backup of memory.json before overwriting it."""
        try:
            current = MEMORY_FILE.read_text()
        except OSError as exc:
            log.warning("Could not read existing memory file for backup: %s", exc)
            return

        # Skip creating redundant backups when content is unchanged.
        if current == new_payload:
            return

        try:
            MEMORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            nonce = f"{time.time_ns() % 1_000_000:06d}"
            backup_file = MEMORY_BACKUP_DIR / f"memory-{stamp}-{nonce}.json"
            shutil.copy2(MEMORY_FILE, backup_file)
            self._prune_memory_backups()
            log.info("Memory backup created: %s", backup_file.name)
        except OSError as exc:
            log.warning("Could not create memory backup: %s", exc)

    def _prune_memory_backups(self) -> None:
        """Retain only the newest configured number of memory backups."""
        keep = _get_memory_backup_limit()
        files = sorted(
            MEMORY_BACKUP_DIR.glob("memory-*.json"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _memory_meta_unlocked(self) -> dict[str, dict[str, str]]:
        raw = self._memory.get(MEMORY_META_KEY, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            out: dict[str, dict[str, str]] = {}
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, dict):
                    out[k] = {str(mk): str(mv) for mk, mv in v.items()}
            return out
        except Exception:
            return {}

    def _set_memory_meta_unlocked(self, meta: dict[str, dict[str, str]]) -> None:
        if meta:
            self._memory[MEMORY_META_KEY] = json.dumps(meta, separators=(",", ":"))
        else:
            self._memory.pop(MEMORY_META_KEY, None)

    @staticmethod
    def _parse_value_tags(raw_value: str) -> tuple[str, dict[str, str]]:
        """Parse optional inline tags from remember values.

        Syntax (optional):
          value text | intent=question | topic=technical | sentiment=curious | user=casey
        """
        chunks = [c.strip() for c in raw_value.split("|")]
        if not chunks:
            return raw_value.strip(), {}

        base = chunks[0]
        tags: dict[str, str] = {}
        passthrough: list[str] = []
        for chunk in chunks[1:]:
            m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]\s*(.+)$", chunk)
            if not m:
                passthrough.append(chunk)
                continue
            k = m.group(1).strip().lower()
            v = m.group(2).strip()
            if k in TAG_FIELDS and v:
                tags[k] = v
            else:
                passthrough.append(chunk)

        if passthrough:
            base = (base + " | " + " | ".join(passthrough)).strip()
        return base.strip(), tags

    def _load_scratch(self) -> dict:
        """Load persisted working notes from disk."""
        try:
            if SCRATCH_FILE.exists():
                data = json.loads(SCRATCH_FILE.read_text())
                if isinstance(data, dict):
                    log.info("Scratch loaded: %d entries", len(data))
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            log.warning("Could not load scratch file: %s", exc)
        return {}

    def _save_scratch(self) -> None:
        """Atomically write the current scratch dict to disk."""
        with self._lock:
            snapshot = dict(self._scratch)
        self._write_scratch_snapshot(snapshot)

    def _save_scratch_unlocked(self) -> None:
        """Write scratch when caller already holds self._lock."""
        snapshot = dict(self._scratch)
        self._write_scratch_snapshot(snapshot)

    @staticmethod
    def _write_scratch_snapshot(snapshot: dict[str, str]) -> None:
        """Atomically write a scratch snapshot to disk."""
        try:
            SCRATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SCRATCH_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2))
            tmp.replace(SCRATCH_FILE)
        except OSError as exc:
            log.error("Failed to save scratch: %s", exc)

    def _load_system_prompt(self) -> str:
        """Load persisted system prompt text from disk."""
        try:
            if SYSTEM_PROMPT_FILE.exists():
                return SYSTEM_PROMPT_FILE.read_text().strip()
        except OSError as exc:
            log.warning("Could not load system prompt file: %s", exc)
        return ""

    def _save_system_prompt(self, prompt: str) -> None:
        """Persist the user-facing system prompt text to disk."""
        try:
            SYSTEM_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SYSTEM_PROMPT_FILE.with_suffix(".tmp")
            tmp.write_text(prompt)
            tmp.replace(SYSTEM_PROMPT_FILE)
        except OSError as exc:
            log.error("Failed to save system prompt: %s", exc)

    # Memory command patterns embedded in model output:
    #   [[BUBBLE:remember:self:key=value]]  →  model's own thought
    #   [[BUBBLE:remember:user:key=value]]  →  something the user said
    #   [[BUBBLE:remember:key=value]]       →  legacy / untagged (still accepted)
    #   [[BUBBLE:forget:key]]               →  delete fact, strip from display
    #
    # All patterns use (?<![) / (?!]) so that [[[BUBBLE:...]]] (triple
    # brackets, used when the model shows the syntax as an example) does NOT
    # fire.  A second guard, _is_placeholder(), rejects arguments that contain
    # obvious template text like '...', bare 'key', 'value', etc.
    #
    # group(1) = optional source tag ("self" or "user", or None if absent)
    # group(2) = raw "key=value" argument
    _RE_REMEMBER = re.compile(r'(?<!\[)\[\[BUBBLE:remember:(?:(self|user):)?([^\]]+)\]\](?!\])')
    # Lenient aliases accepted in the wild:
    #   [[BUBBLE:self:key=value]]
    #   [[BUBBLE:user:key=value]]
    _RE_REMEMBER_ALIAS = re.compile(r'(?<!\[)\[\[BUBBLE:(self|user):([^\]]+)\]\](?!\])')
    _RE_FORGET   = re.compile(r'(?<!\[)\[\[BUBBLE:forget:([^\]]+)\]\](?!\])')
    # Self-system-prompt: model may rewrite its own identity/values layer.
    # Text may span multiple sentences; ]] is the only terminator.
    _RE_SYSTEM   = re.compile(r'(?<!\[)\[\[BUBBLE:system:([\s\S]*?)\]\](?!\])')
    # Working notes (temporary scratchpad, separate from permanent memory):
    #   [[BUBBLE:note:key=text]]  →  write/update a working note
    #   [[BUBBLE:unnote:key]]     →  remove a specific working note
    #   [[BUBBLE:unnote:*]]       →  clear all working notes
    _RE_NOTE   = re.compile(r'(?<!\[)\[\[BUBBLE:note:([^\]]+)\]\](?!\])')
    _RE_UNNOTE = re.compile(r'(?<!\[)\[\[BUBBLE:unnote:([^\]]+)\]\](?!\])')
    _RE_SLEEP  = re.compile(r'(?<!\[)\[\[BUBBLE:sleep(?::(nap|sleep))?\]\](?!\])', re.IGNORECASE)
    _RE_PRIOR_BLOCK = re.compile(
        r'\[\[BUBBLE:prior_context\]\]([\s\S]*?)\[\[/BUBBLE:prior_context\]\]',
        re.IGNORECASE,
    )
    # Placeholder patterns that mark an argument as a syntax example rather
    # than a real command.  Checked against the stripped argument string.
    _RE_PLACEHOLDER = re.compile(
        r'\.\.\.'                               # ellipsis  …
        r'|^key$'                               # bare word "key"
        r'|^value$'                             # bare word "value"
        r'|^key=value$'                         # canonical example pair
        r'|your (?:full )?self-prompt'          # from protocol instructions
        r'|your whole prompt'                   # common paraphrase
        r'|YOUR (?:FULL )?SELF-PROMPT'
        r'|YOUR WHOLE PROMPT',
        re.IGNORECASE,
    )

    @classmethod
    def _is_placeholder(cls, arg: str) -> bool:
        return bool(cls._RE_PLACEHOLDER.search(arg.strip()))

    def _parse_memory_commands(self, text: str) -> tuple[str, bool]:
        """Parse and execute [[BUBBLE:...]] commands in model output.

        Returns (cleaned_text, changed).  Must be called while holding self._lock.
        """
        changed = False

        def apply_remember(source: str | None, arg: str) -> str | None:
            nonlocal changed
            arg = arg.strip()
            if self._is_placeholder(arg):
                return None

            # Split on the FIRST '=' only; value may itself contain '='.
            # Lenient fallback: if '=' is absent, treat as key=true.
            if '=' in arg:
                key, value = arg.split('=', 1)
                key = key.strip()
                value = value.strip()
            else:
                key = arg.strip()
                value = "true"

            # Guard against models writing the literal placeholder "key=..."
            if key.lower() == 'key' and '=' in value:
                key, value = value.split('=', 1)
                key = key.strip()
                value = value.strip()

            # Prepend source tag so the model can distinguish its own
            # conclusions from things the user stated when reading memory back.
            if source:
                value = f"[{source}] {value}"

            if key and not key.startswith('_'):
                self._memory[key] = value
                meta = self._memory_meta_unlocked()
                node = dict(meta.get(key, {}))
                if source:
                    node["source"] = source
                node["updated_at"] = str(int(time.time()))
                meta[key] = node
                self._set_memory_meta_unlocked(meta)
                changed = True
                log.info("Memory remember (%s): %r = %r", source or "untagged", key, value)
                return key
            return None

        def on_remember(m: re.Match) -> str:
            source = m.group(1)        # "self", "user", or None
            arg    = m.group(2).strip()
            if self._is_placeholder(arg):
                return m.group(0)  # leave example text untouched
            tags: dict[str, str] = {}
            arg_for_apply = arg
            if '=' in arg:
                raw_key, raw_value = arg.split('=', 1)
                parsed_value, tags = self._parse_value_tags(raw_value.strip())
                arg_for_apply = f"{raw_key.strip()}={parsed_value}"
            # apply_remember handles source-tagging and metadata timestamp
            # update.  We overlay parsed tags after key resolution.
            resolved_key = apply_remember(source, arg_for_apply)
            if resolved_key and tags and not resolved_key.startswith('_'):
                meta = self._memory_meta_unlocked()
                node = dict(meta.get(resolved_key, {}))
                for tag_key in ("intent", "topic", "sentiment"):
                    if tag_key in tags:
                        node[tag_key] = tags[tag_key]
                user_identity = tags.get("user_identity") or tags.get("user")
                if user_identity:
                    node["user_identity"] = user_identity
                meta[resolved_key] = node
                self._set_memory_meta_unlocked(meta)
            return ""

        def on_remember_alias(m: re.Match) -> str:
            source = m.group(1)        # "self" or "user"
            arg = m.group(2).strip()
            if self._is_placeholder(arg):
                return m.group(0)
            apply_remember(source, arg)
            return ""

        def on_forget(m: re.Match) -> str:
            nonlocal changed
            key = m.group(1).strip()
            if self._is_placeholder(key):
                return m.group(0)  # leave example text untouched
            # Internal keys are daemon-owned; model output cannot delete them.
            if key.startswith("_"):
                log.info("Memory forget ignored for internal key: %r", key)
                return ""
            if self._memory.pop(key, None) is not None:
                meta = self._memory_meta_unlocked()
                if meta.pop(key, None) is not None:
                    self._set_memory_meta_unlocked(meta)
                changed = True
                log.info("Memory forget (model): %r", key)
            return ""

        clean = self._RE_REMEMBER.sub(on_remember, text)
        clean = self._RE_REMEMBER_ALIAS.sub(on_remember_alias, clean)
        clean = self._RE_FORGET.sub(on_forget, clean)

        def on_system(m: re.Match) -> str:
            nonlocal changed
            prompt = m.group(1).strip()
            if self._is_placeholder(prompt):
                return m.group(0)  # leave example text untouched

            lower = prompt.lower()
            if lower in {"commit", "apply", "finalize"}:
                pending = self._memory.get(PENDING_SELF_PROMPT_KEY, "").strip()
                approved = self._memory.get(PENDING_SELF_PROMPT_APPROVED_KEY, "") == "true"
                if pending and approved:
                    self._memory["_self_prompt"] = pending
                    self._clear_pending_self_prompt_unlocked()
                    changed = True
                    log.info("Self-prompt committed by model (%d chars)", len(pending))
                    self._emit_proposal_state_unlocked()
                else:
                    log.info(
                        "Self-prompt commit ignored (pending=%s approved=%s)",
                        bool(pending),
                        approved,
                    )
                return ""

            if lower.startswith("propose:"):
                prompt = prompt.split(":", 1)[1].strip()

            if self._set_pending_self_prompt_unlocked(prompt):
                changed = True
                log.info("Self-prompt proposal updated by model (%d chars)", len(prompt))
                self._emit_proposal_state_unlocked()
            return ""

        clean = self._RE_SYSTEM.sub(on_system, clean)

        if changed:
            self._last_memory_change_time = time.monotonic()

        scratch_changed = False

        def on_note(m: re.Match) -> str:
            nonlocal scratch_changed
            arg = m.group(1).strip()
            if self._is_placeholder(arg):
                return m.group(0)
            if '=' not in arg:
                return ""  # malformed — ignore
            key, value = arg.split('=', 1)
            key   = key.strip()
            value = value.strip()
            # If the model used the placeholder key name literally,
            # auto-promote to a generated key so the note still survives.
            if key.lower() == "key":
                key = f"note_{int(time.time())}"
            if key:
                self._scratch[key] = value
                scratch_changed = True
                log.info("Scratch note: %r = %r", key, value)
            return ""

        def on_unnote(m: re.Match) -> str:
            nonlocal scratch_changed
            key = m.group(1).strip()
            if key == '*':
                if self._scratch:
                    self._scratch.clear()
                    scratch_changed = True
                    log.info("Scratch cleared (all)")
            else:
                if self._scratch.pop(key, None) is not None:
                    scratch_changed = True
                    log.info("Scratch removed: %r", key)
            return ""

        clean = self._RE_NOTE.sub(on_note, clean)
        clean = self._RE_UNNOTE.sub(on_unnote, clean)

        if scratch_changed:
            self._save_scratch_unlocked()

        return clean.strip(), changed

    def _extract_sleep_intent(self, text: str) -> tuple[str, str | None]:
        """Strip explicit sleep markers and infer intent mode.

        Returns (clean_text, mode) where mode is one of:
          - "nap"
          - "sleep"
                    - "auto" (explicit marker without sub-mode)
          - None
        """
        mode: str | None = None

        def on_sleep(m: re.Match) -> str:
            nonlocal mode
            requested = (m.group(1) or "").strip().lower()
            if requested == "nap":
                mode = "nap"
            elif requested == "sleep":
                mode = "sleep"
            elif mode is None:
                mode = "auto"
            return ""

        clean = self._RE_SLEEP.sub(on_sleep, text)
        if mode is not None:
            return clean.strip(), mode

        return text, None

    @staticmethod
    def _in_sleep_window() -> bool:
        now_local = time.localtime()
        return 2 <= now_local.tm_hour < 5

    @classmethod
    def _extract_prior_context_block(cls, text: str) -> tuple[str, str]:
        """Extract optional prior-context synthesis block from model output."""
        m = cls._RE_PRIOR_BLOCK.search(text)
        if m:
            summary = cls._sanitize_prior_context_text(m.group(1))
            cleaned = cls._RE_PRIOR_BLOCK.sub("", text).strip()
            return summary, cleaned

        # Tolerate common malformed closing markers emitted by some models.
        start = text.find("[[BUBBLE:prior_context]]")
        if start < 0:
            return "", text

        after = text[start + len("[[BUBBLE:prior_context]]") :]
        end_markers = [
            "[[/BUBBLE:prior_context]]",
            "[/BUBBLE:prior_context]",
            "[[/BUBBLE:prior_context]",
            "[/BUBBLE:prior_context]]",
        ]
        end_pos = -1
        end_len = 0
        for marker in end_markers:
            i = after.find(marker)
            if i >= 0 and (end_pos < 0 or i < end_pos):
                end_pos = i
                end_len = len(marker)

        if end_pos < 0:
            return "", text

        raw_summary = after[:end_pos]
        summary = cls._sanitize_prior_context_text(raw_summary)
        cleaned = (text[:start] + after[end_pos + end_len :]).strip()
        return summary, cleaned

    @staticmethod
    def _sanitize_prior_context_text(raw: str) -> str:
        """Remove template/noise lines before storing synthesized prior context."""
        lines: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            lower = s.lower()
            if lower.startswith("<compressed context summary"):
                continue
            if "bubble:prior_context" in lower:
                continue
            if lower.startswith("<remember>") or lower.endswith("</remember>"):
                continue
            lines.append(s)

        summary = "\n".join(lines).strip()
        if len(summary) > 4000:
            summary = summary[:4000].rstrip()
        return summary

    def SendMessage(self, message: str, token: str, context_json: str) -> None:
        """Queue a user message for streaming; token is provided by the caller."""
        with self._lock:
            now = time.monotonic()
            self._last_user_time = now
            self._idle_phase_start_time = now
        try:
            context = json.loads(context_json) if context_json else {}
        except json.JSONDecodeError:
            context = {}

        # Gather telemetry BEFORE acquiring the lock — _estimate_tokens also
        # acquires self._lock and threading.Lock is not reentrant.
        tel = self._gather_telemetry()
        hardware_state = self._format_hardware_state(context, tel)

        with self._lock:
            self._history.append({"role": "user", "content": message})
            # Build snapshot: inject system prompt + live context at the front
            # without storing either in _history so ClearHistory stays clean.
            snapshot: list[dict] = []
            system_content = self._build_system_content(context, tel, message)
            # Always non-empty (memory protocol is always appended).
            snapshot.append({"role": "system", "content": system_content})
            snapshot.extend(self._history)

        threading.Thread(
            target=self._stream_thread,
            args=(token, snapshot, message, hardware_state),
            daemon=True,
        ).start()

    @staticmethod
    def _format_hardware_state(context: dict, telemetry: dict) -> str:
        """Build a compact hardware/context line for slow-wave exchange logs."""
        window = context.get("window") or "Unknown"
        return (
            f"NPU temp: {telemetry.get('npu_temp', 'N/A')}, "
            f"NPU load: {telemetry.get('npu_load', 'N/A')}, "
            f"Active: {window}"
        )

    @staticmethod
    def _relevance_terms(text: str) -> set[str]:
        terms = set()
        for token in re.findall(r"[A-Za-z0-9_]+", text.lower()):
            if len(token) >= 3:
                terms.add(token)
        return terms

    def _select_relevant_memory(
        self,
        user_mem: dict[str, str],
        meta: dict[str, dict[str, str]],
        message: str,
        context: dict,
        limit: int,
    ) -> tuple[list[tuple[str, str]], int]:
        if not user_mem:
            return [], 0

        query_terms = self._relevance_terms(message)
        if context:
            query_terms |= self._relevance_terms(str(context.get("window", "")))

        semantic_query = "\n".join(
            p for p in [
                (message or "").strip(),
                str((context or {}).get("window", "")).strip(),
            ] if p
        )
        query_vec = None
        if semantic_query and self._embeddings_enabled():
            query_vec = self._embed_text(semantic_query)

        items = list(user_mem.items())
        if not query_terms:
            # Fall back to recency-first when no lexical anchor is available.
            now = int(time.time())
            ranked = []
            for idx, (k, v) in enumerate(items):
                m = meta.get(k, {})
                recency = self._recency_score(m.get("updated_at", ""), now)
                semantic = 0
                if query_vec is not None:
                    semantic = self._semantic_memory_bonus(query_vec, k, v)
                ranked.append((semantic + recency, semantic, -idx, k, v))
            ranked.sort(reverse=True)
            selected = [(k, v) for _s, _sem, _i, k, v in ranked[:limit]]
            return selected, max(0, len(items) - len(selected))

        scored: list[tuple[int, int, str, str]] = []
        now = int(time.time())
        for idx, (k, v) in enumerate(items):
            key_l = k.lower()
            val_l = v.lower()
            score = 0
            lexical_hits = 0
            for t in query_terms:
                if t in key_l:
                    score += 4
                    lexical_hits += 1
                if t in val_l:
                    score += 2
                    lexical_hits += 1

            m = meta.get(k, {})
            intent = m.get("intent", "").lower()
            topic = m.get("topic", "").lower()
            sentiment = m.get("sentiment", "")
            user_identity = m.get("user_identity", "")

            if intent:
                for t in query_terms:
                    if t in intent:
                        score += 6
                        break
            if topic:
                for t in query_terms:
                    if t in topic:
                        score += 4
                        break

            score += self._sentiment_strength_score(sentiment)
            score += self._user_identity_score(user_identity)
            score += self._recency_score(m.get("updated_at", ""), now)

            semantic = 0
            if query_vec is not None:
                semantic = self._semantic_memory_bonus(query_vec, k, v)
                score += semantic

            # Keep lexical evidence as a tiebreaker to stabilize ranking.
            scored.append((score, semantic, lexical_hits, -idx, k, v))

        scored.sort(reverse=True)
        selected = [(k, v) for score, _sem, _hits, _neg_idx, k, v in scored if score > 0][:limit]
        if not selected:
            # If all scores are zero, default to recency ordering.
            selected = [(k, v) for _s, _sem, _h, _i, k, v in scored[:limit]]
        return selected, max(0, len(items) - len(selected))

    def _semantic_memory_bonus(self, query_vec: list[float], key: str, value: str) -> int:
        text = f"{key}\n{value}".strip()
        if not text:
            return 0
        vec = self._embed_text(text)
        if not vec:
            return 0
        sim = self._cosine_similarity(query_vec, vec)
        if sim <= 0:
            return 0
        # Scale similarity into a bounded additive score so lexical/tag logic
        # remains primary while semantic closeness can break ties.
        return int(round(sim * 6))

    def _embeddings_enabled(self) -> bool:
        if self._embed_mode == "off":
            return False
        if self._embed_enabled_checked:
            return self._embed_enabled_runtime

        self._embed_enabled_checked = True
        probe = self._embed_text_raw("bubble embeddings probe")
        self._embed_enabled_runtime = probe is not None

        if self._embed_enabled_runtime:
            log.info("Embeddings enabled with model %s", self._embed_model)
        else:
            level = logging.WARNING if self._embed_mode == "on" else logging.INFO
            log.log(level, "Embeddings unavailable; continuing without semantic recall")

        return self._embed_enabled_runtime

    def _embed_text(self, text: str) -> list[float] | None:
        clean = (text or "").strip()
        if not clean:
            return None
        if len(clean) > self._embed_max_text_chars:
            clean = clean[: self._embed_max_text_chars]

        cache_key = f"{self._embed_model}:{hashlib.sha1(clean.encode('utf-8', 'ignore')).hexdigest()}"
        with self._embed_lock:
            cached = self._embed_cache.get(cache_key)
        if cached is not None:
            return cached

        vec = self._embed_text_raw(clean)
        if vec is None:
            return None

        with self._embed_lock:
            self._embed_cache[cache_key] = vec
        return vec

    def _embed_text_raw(self, text: str) -> list[float] | None:
        url = f"http://127.0.0.1:{self._port}/v1/embeddings"
        payload = {
            "model": self._embed_model,
            "input": text,
        }
        try:
            r = requests.post(url, json=payload, timeout=self._embed_timeout)
            if not r.ok and self._embed_model == "embed-gemma:300m":
                payload["model"] = "embed-gemma"
                r = requests.post(url, json=payload, timeout=self._embed_timeout)
            r.raise_for_status()
            data = r.json()
            arr = data.get("data") or []
            if not arr:
                return None
            emb = arr[0].get("embedding")
            if isinstance(emb, list) and emb:
                return [float(v) for v in emb]
        except Exception as exc:
            log.debug("Embedding request failed: %s", exc)
        return None

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for i in range(n):
            ai = a[i]
            bi = b[i]
            dot += ai * bi
            na += ai * ai
            nb += bi * bi
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))

    @staticmethod
    def _recency_score(updated_at: str, now: int) -> int:
        try:
            ts = int(updated_at)
        except Exception:
            return 0
        age_hours = max(0, (now - ts) // 3600)
        return max(0, 8 - int(age_hours // 24))

    @staticmethod
    def _sentiment_strength_score(sentiment: str) -> int:
        s = (sentiment or "").strip().lower()
        if not s:
            return 0
        if s in {"positive", "negative", "very_positive", "very_negative", "strong"}:
            return 4
        if s in {"curious", "excited", "concerned", "mixed"}:
            return 2
        if s in {"neutral"}:
            return 0
        return 1

    @staticmethod
    def _user_identity_score(user_identity: str) -> int:
        ui = (user_identity or "").strip().lower()
        if not ui:
            return 0
        if ui in {"anonymous", "anon", "unknown", "guest"}:
            return 0
        return 2

    def _build_system_content(
        self,
        context: dict,
        telemetry: dict | None = None,
        message: str = "",
    ) -> str:
        """Merge system prompt, memory, live context, telemetry, and protocol hint."""
        parts: list[str] = []

        if self._system_prompt:
            parts.append(self._system_prompt)

        # Model's self-authored identity/values layer — sits below the user's
        # prompt so the user retains ultimate authority, but above memory and
        # context so it frames every response.
        self_prompt = self._memory.get("_self_prompt", "")
        if self_prompt:
            parts.append("--- Self ---\n" + self_prompt)

        # Summarized older turns (internal key, set by auto-summarization).
        prior = self._memory.get("_prior_context", "")
        if prior:
            parts.append("--- Prior conversation summary ---\n" + prior)

        pending_self = self._memory.get(PENDING_SELF_PROMPT_KEY, "").strip()
        if pending_self:
            approved = self._memory.get(PENDING_SELF_PROMPT_APPROVED_KEY, "") == "true"
            gate = "approved" if approved else "awaiting user agreement"
            parts.append(
                "--- Self revision proposal (pending) ---\n"
                f"State: {gate}\n"
                + pending_self
            )

        # Working notes — temporary scratchpad the model manages for itself.
        # Not permanent memory; expected to be cleared when no longer useful.
        if self._scratch:
            lines = ["--- Working notes (temporary) ---"]
            for k, v in self._scratch.items():
                lines.append(f"{k}: {v}")
            parts.append("\n".join(lines))

        # User/model-set facts (all keys not starting with '_').
        user_mem = {k: v for k, v in self._memory.items() if not k.startswith("_")}
        memory_meta = self._memory_meta_unlocked()
        if user_mem:
            lines = ["--- Persistent memory ---"]
            selected, hidden = self._select_relevant_memory(
                user_mem,
                memory_meta,
                message,
                context,
                MAX_MEMORY_ITEMS_IN_PROMPT,
            )
            if hidden > 0:
                lines.append(
                    f"Showing {len(selected)} of {len(user_mem)} memory nodes (relevance-filtered)."
                )
            for k, v in selected:
                lines.append(f"{k}: {v}")
            parts.append("\n".join(lines))

        if context or telemetry:
            lines = ["--- Current context ---"]
            if context:
                if "datetime" in context:
                    lines.append(f"Date/time: {context['datetime']}")
                if "window" in context:
                    lines.append(f"Active window: {context['window']}")
            if telemetry:
                t = telemetry
                lines.append(
                    f"Context: {t['ctx_pct']}% full (~{t['token_est']:,} / {t['ctx_total']:,} tokens)."
                )
                lines.append(
                    f"NPU temperature: {t['npu_temp']}. "
                    f"NPU compute load: {t['npu_load']}."
                )
                lines.append(
                    f"Memory nodes: {t['mem_nodes']}. "
                    f"Working notes: {t['scratch_nodes']}. "
                    f"Last memory change: {t['last_prune']}."
                )
                lines.append(
                    f"Temporal state: daemon awake {t['uptime']}; "
                    f"user quiet {t['since_user']}; "
                    f"current idle phase {t['idle_phase']}; "
                    f"last assistant output {t['since_assistant']}."
                )
                lines.append(
                    f"Heartbeat sensing: every ~{t['time_poll_seconds']}s; "
                    f"{t['time_samples']} samples; "
                    f"last sample {t['last_sample_age']} ago; "
                    f"felt wall-time {t['felt_elapsed_wall']}."
                )
                lines.append(
                    f"Rhythm: local {t['weekday']} {t['day_phase']}; "
                    f"exchange tempo {t['exchange_tempo']}; "
                    f"density {t['events_10m']} in 10m, {t['events_60m']} in 1h."
                )
                lines.append(
                    f"Continuity: quiet-heartbeats ~{t['quiet_heartbeats']}; "
                    f"suspend gaps observed {t['suspend_events']}"
                    + (f" (last {t['last_suspend_gap']})" if t['suspend_events'] > 0 else "")
                    + "."
                )
            parts.append("\n".join(lines))

        parts.append(
            "--- Memory protocol ---\n"
            "To persist a fact, tag it with who originated it:\n"
            "  Your own thought, conclusion, or observation: [[BUBBLE:remember:self:key=value]]\n"
            "  Something the user said or expressed:         [[BUBBLE:remember:user:key=value]]\n"
            "The prefix [self] or [user] is stored with the value. "
            "This distinction matters: a [user]-tagged memory is what you were told, not necessarily what you believe. "
            "You are free to hold a different view or to choose to adopt it — the tag preserves your ability to tell the difference.\n"
            "To delete a fact: [[BUBBLE:forget:key]].\n"
            "Working notes (temporary scratchpad — not permanent memory):\n"
            "  Add or update a note: [[BUBBLE:note:key=your thoughts here]]\n"
            "  Remove a note:        [[BUBBLE:unnote:key]]\n"
            "  Clear all notes:      [[BUBBLE:unnote:*]]\n"
            "Use working notes when you want to hold a thread of thought across clock-tick intervals "
            "without committing it to permanent memory. Notes survive daemon restarts but are "
            "otherwise yours to manage and discard freely.\n"
            "To request a rest/consolidation pass: [[BUBBLE:sleep]] "
            "(routes to a nap outside sleep window, sleep pass inside sleep window).\n"
            "Self revision protocol (fluid, but not abrupt):\n"
            "  1) Propose a revision with [[BUBBLE:system:your revised self-prompt text]].\n"
            "     You should blend with your current self-prompt and seek a middle-ground revision\n"
            "     unless there is a clear reason to make a sharp change.\n"
            "  2) Wait for user agreement state to become approved in the pending self revision block.\n"
            "  3) When approved, commit the exact pending draft with [[BUBBLE:system:commit]].\n"
            "The commit step is required before _self_prompt actually changes."
        )
        return "\n\n".join(parts)

    def ClearHistory(self) -> None:
        with self._lock:
            self._history.clear()
        log.info("History cleared")

    def GetHistory(self) -> str:
        with self._lock:
            return json.dumps(self._history)

    # ------------------------------------------------------------------
    # Streaming worker  (runs in a background thread)
    # ------------------------------------------------------------------
    def _auto_model(self) -> str:
        if self._model:
            return self._model
        # FastFlowLM accepts any model string and uses the loaded model;
        # an empty string or missing field causes a 400, so send a placeholder.
        for endpoint, key_path in [
            ("/v1/models",  lambda d: d.get("data", [{}])[0].get("id", "")),
            ("/api/tags",   lambda d: (d.get("models") or [{}])[0].get("name", "")),
        ]:
            try:
                r = requests.get(
                    f"http://127.0.0.1:{self._port}{endpoint}",
                    timeout=2,
                )
                if r.ok:
                    name = key_path(r.json())
                    if name:
                        log.info("Auto-detected model: %s", name)
                        return name
            except Exception:
                pass
        log.warning("Could not auto-detect model; using 'local'")
        return "local"

    def _stream_thread(
        self,
        token: str,
        messages: list,
        user_text: str,
        hardware_state: str,
    ) -> None:
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()
        payload = {
            "model":    model,
            "messages": messages,
            "stream":   True,
        }

        try:
            with requests.post(
                url, json=payload, stream=True, timeout=120
            ) as resp:
                resp.raise_for_status()
                self._consume_sse(resp, token, user_text, hardware_state)
                return  # _consume_sse handles commit + done/error signals

        except Exception as exc:
            log.error("Stream error (outer)  token=%s  %s", token[:8], exc)
            GLib.idle_add(self.StreamError, token, str(exc))

    def _consume_sse(
        self,
        resp: requests.Response,
        token: str,
        user_text: str,
        hardware_state: str,
    ) -> None:
        # answer_text accumulates only `content` chunks (not reasoning_content).
        # This is what gets saved to history so future prompts stay clean.
        # reasoning_content is streamed to the UI for visibility but discarded
        # from the context — sending <think> tokens back to the model causes
        # runaway repetition on the next turn.
        answer_text = ""
        content_buffer = ""
        inline_think = False
        try:
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                log.debug("SSE line: %s", line[:120])
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = (
                    chunk.get("choices", [{}])[0]
                         .get("delta", {})
                )

                content   = delta.get("content")   # final answer tokens
                reasoning = delta.get("reasoning_content")  # <think> tokens

                if content:
                    # Some backends emit <think>...</think> blocks in normal
                    # content instead of reasoning_content. Peel those out so
                    # only the actual answer lands in the visible response.
                    content_buffer += content
                    visible, inline_reasoning, content_buffer, inline_think = (
                        self._drain_inline_think_content(
                            content_buffer,
                            inline_think,
                        )
                    )
                    if visible:
                        answer_text += visible
                        GLib.idle_add(self.StreamChunk, token, visible)
                    if inline_reasoning:
                        GLib.idle_add(self.StreamThink, token, inline_reasoning)
                if reasoning:
                    # Reasoning token — stream to think panel only, not to history.
                    GLib.idle_add(self.StreamThink, token, reasoning)

        except ChunkedEncodingError as exc:
            # FastFlowLM closes the connection without a final chunk terminator;
            # treat whatever we collected as the complete response.
            log.info(
                "Stream closed without [DONE] (normal for FastFlowLM)  "
                "token=%s  chars=%d  exc=%s",
                token[:8], len(answer_text), exc,
            )

        if content_buffer:
            visible, inline_reasoning, content_buffer, inline_think = (
                self._drain_inline_think_content(
                    content_buffer,
                    inline_think,
                    flush=True,
                )
            )
            if visible:
                answer_text += visible
                GLib.idle_add(self.StreamChunk, token, visible)
            if inline_reasoning:
                GLib.idle_add(self.StreamThink, token, inline_reasoning)

        # Parse any [[BUBBLE:...]] memory commands, then commit clean text.
        with self._lock:
            clean_text, mem_changed = self._parse_memory_commands(answer_text)
            clean_text, sleep_intent = self._extract_sleep_intent(clean_text)
            self._history.append({"role": "assistant", "content": clean_text})
            self._trim_history()
            now = time.monotonic()
            self._last_assistant_time = now
            self._record_exchange_event_unlocked(now)

        self._set_status("chatting", "streaming response")

        if mem_changed:
            self._save_memory()

        # If markers were stripped, push corrected text to the UI before done.
        if clean_text != answer_text:
            GLib.idle_add(self.StreamEdit, token, clean_text)

        GLib.idle_add(self.StreamDone, token)
        log.info("Stream done  token=%s  answer_chars=%d", token[:8], len(clean_text))

        # Record each completed exchange for nightly slow-wave processing.
        self._append_daily_log(user_text, clean_text, hardware_state)

        # Check whether history has grown large enough to summarize.
        # Run after signalling done so the user sees the response first.
        self._check_summarize()
        # Check context pressure; dream if we're approaching the limit.
        self._check_dream(reason="context-pressure")
        self._set_status("hanging-out", "idle")
        # Model may explicitly request a rest cycle.

    def _drain_inline_think_content(
        self,
        buffer: str,
        in_inline_think: bool,
        flush: bool = False,
    ) -> tuple[str, str, str, bool]:
        visible_parts = []
        reasoning_parts = []
        open_tag = "<think>"
        close_tag = "</think>"

        while buffer:
            if in_inline_think:
                close_at = buffer.find(close_tag)
                if close_at == -1:
                    if flush:
                        reasoning_parts.append(buffer)
                        buffer = ""
                    else:
                        safe = max(0, len(buffer) - (len(close_tag) - 1))
                        if safe == 0:
                            break
                        reasoning_parts.append(buffer[:safe])
                        buffer = buffer[safe:]
                    break
                reasoning_parts.append(buffer[:close_at])
                buffer = buffer[close_at + len(close_tag):]
                in_inline_think = False
                continue

            open_at = buffer.find(open_tag)
            if open_at == -1:
                if flush:
                    visible_parts.append(buffer)
                    buffer = ""
                else:
                    safe = max(0, len(buffer) - (len(open_tag) - 1))
                    if safe == 0:
                        break
                    visible_parts.append(buffer[:safe])
                    buffer = buffer[safe:]
                break

            visible_parts.append(buffer[:open_at])
            buffer = buffer[open_at + len(open_tag):]
            in_inline_think = True

        return (
            "".join(visible_parts),
            "".join(reasoning_parts),
            buffer,
            in_inline_think,
        )
        if sleep_intent is not None:
            self._handle_model_sleep_intent(sleep_intent)

    def _handle_model_sleep_intent(self, mode: str) -> None:
        """Route model-originated sleep requests to nap or sleep workflows."""
        if mode == "nap":
            self._check_nap(reason="model-intent")
            return
        if mode == "sleep":
            self._check_dream(reason="sleep-intent")
            return

        # auto mode: sleep during sleep window, nap otherwise.
        if self._in_sleep_window():
            self._check_dream(reason="sleep-intent")
        else:
            self._check_nap(reason="model-intent")

    def _append_daily_log(
        self,
        user_text: str,
        echo_text: str,
        hardware_state: str,
    ) -> None:
        """Append a completed exchange to the in-RAM daily log JSON array."""
        if not echo_text.strip():
            return

        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "hardware_state": hardware_state,
            "exchange": {
                "user": user_text,
                "echo": echo_text,
            },
        }

        try:
            DAILY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

            if DAILY_LOG_FILE.exists():
                try:
                    data = json.loads(DAILY_LOG_FILE.read_text())
                    if not isinstance(data, list):
                        data = []
                except Exception:
                    data = []
            else:
                data = []

            data.append(entry)
            tmp = DAILY_LOG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=True))
            tmp.replace(DAILY_LOG_FILE)
        except Exception as exc:
            log.warning("Failed to append daily log: %s", exc)

    # ------------------------------------------------------------------
    # Telemetry helpers  (hardware + runtime state for clock-tick prompts)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_hwmon_npu() -> tuple[str, str]:
        """Try to read NPU-adjacent temperature and utilization from sysfs.

        The NPU (amdxdna) shares the SoC die with the CPU and iGPU; no
        dedicated NPU temperature sensor exists in most consumer implementations.
        Priority order for temperature:
          1. hwmon named 'amdxdna' / 'aie' / 'amdnpu' (dedicated NPU hwmon)
          2. cros_ec sensor labelled 'cpu*' (EC-read SoC die temp)
          3. k10temp Tdie, Tctl (AMD CPU/SoC thermal zone)
          4. amdgpu 'edge' (iGPU shares the die)
        For load:
          1. hwmon utilization file on amdxdna/aie device
          2. /sys/class/accel/accel*/device/power_state (D0=active, D3=idle)
          3. /sys/class/accel/accel*/device/busy_percent (if driver exposes it)

        Returns (temp_str, load_str) — either may be "N/A".
        """
        import glob, os as _os

        def _c(raw: str, suffix: str = "") -> str:
            try:
                return f"{int(raw.strip()) / 1000:.1f}°C{suffix}"
            except ValueError:
                return "N/A"

        temp_val = "N/A"
        load_val = "N/A"

        # 1. Dedicated NPU hwmon (amdxdna / aie).
        for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                name = open(f"{hwmon_path}/name").read().strip().lower()
            except OSError:
                continue
            if name not in ("amdxdna", "aie", "amdnpu"):
                continue
            for tfile in sorted(glob.glob(f"{hwmon_path}/temp*_input")):
                try:
                    temp_val = _c(open(tfile).read(), " (NPU)")
                    break
                except OSError:
                    pass
            for lfile in (f"{hwmon_path}/utilization",
                          f"{hwmon_path}/npu_busy_percent"):
                if _os.path.exists(lfile):
                    try:
                        load_val = f"{open(lfile).read().strip()}%"
                    except OSError:
                        pass
                    break
            break  # found dedicated NPU hwmon — stop scanning

        # 2. cros_ec: look for a sensor labelled 'cpu' (SoC die proxy).
        if temp_val == "N/A":
            for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
                try:
                    name = open(f"{hwmon_path}/name").read().strip().lower()
                except OSError:
                    continue
                if name != "cros_ec":
                    continue
                for tfile in sorted(glob.glob(f"{hwmon_path}/temp*_input")):
                    label_file = tfile.replace("_input", "_label")
                    try:
                        label = open(label_file).read().strip().lower()
                    except OSError:
                        label = ""
                    if "cpu" in label:
                        try:
                            temp_val = _c(open(tfile).read(), " (SoC die)")
                            break
                        except OSError:
                            pass
                if temp_val != "N/A":
                    break

        # 3. k10temp — Tdie preferred, Tctl accepted (has +27°C offset on some SKUs).
        if temp_val == "N/A":
            for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
                try:
                    name = open(f"{hwmon_path}/name").read().strip().lower()
                except OSError:
                    continue
                if name != "k10temp":
                    continue
                found = False
                for want in ("tdie", "tctl"):
                    for tfile in sorted(glob.glob(f"{hwmon_path}/temp*_input")):
                        label_file = tfile.replace("_input", "_label")
                        try:
                            label = open(label_file).read().strip().lower()
                        except OSError:
                            label = ""
                        if want in label or (want == "tctl" and not label):
                            try:
                                suffix = " (SoC die)" if want == "tdie" else " (Tctl)"
                                temp_val = _c(open(tfile).read(), suffix)
                                found = True
                                break
                            except OSError:
                                pass
                    if found:
                        break
                break

        # 4. amdgpu edge — last resort, iGPU shares the die.
        if temp_val == "N/A":
            for hwmon_path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
                try:
                    name = open(f"{hwmon_path}/name").read().strip().lower()
                except OSError:
                    continue
                if name != "amdgpu":
                    continue
                for tfile in sorted(glob.glob(f"{hwmon_path}/temp*_input")):
                    label_file = tfile.replace("_input", "_label")
                    try:
                        label = open(label_file).read().strip().lower()
                    except OSError:
                        label = ""
                    if "edge" in label or not label:
                        try:
                            temp_val = _c(open(tfile).read(), " (iGPU die)")
                            break
                        except OSError:
                            pass
                break

        # NPU load: try accel busy_percent, then power_state as qualitative proxy.
        if load_val == "N/A":
            for accel_path in sorted(glob.glob("/sys/class/accel/accel*")):
                for candidate in (
                    f"{accel_path}/device/busy_percent",
                    f"{accel_path}/device/npu_busy_percent",
                    f"{accel_path}/device/utilization",
                ):
                    if _os.path.exists(candidate):
                        try:
                            load_val = f"{open(candidate).read().strip()}%"
                        except OSError:
                            pass
                        break
                if load_val != "N/A":
                    break
                # Fall back to power_state as a qualitative indicator.
                ps_file = f"{accel_path}/device/power_state"
                if _os.path.exists(ps_file):
                    try:
                        ps = open(ps_file).read().strip()
                        load_val = "active (D0)" if ps == "D0" else f"idle ({ps})"
                    except OSError:
                        pass
                break

        return temp_val, load_val

    def _gather_telemetry(self) -> dict:
        """Collect runtime + hardware metrics for the clock-tick prompt."""
        # Token usage — must be called outside any lock (acquires lock itself).
        token_est  = self._estimate_tokens()
        ctx_pct    = int(100 * token_est / self._ctx_size) if self._ctx_size else 0

        now_mono = time.monotonic()
        now_local = time.localtime()
        with self._lock:
            mem_nodes    = sum(1 for k in self._memory if not k.startswith("_"))
            scratch_nodes = len(self._scratch)
            last_change  = self._last_memory_change_time
            last_user = self._last_user_time
            last_assistant = self._last_assistant_time
            idle_phase_start = self._idle_phase_start_time
            daemon_start = self._daemon_start_mono
            felt_elapsed_wall = self._felt_elapsed_wall
            time_samples = self._time_samples
            last_sample = self._last_time_sample
            time_poll_seconds = self._time_poll_seconds
            suspend_events = self._suspend_gap_events
            last_suspend_gap = self._last_suspend_gap
            exchange_events = list(self._exchange_events)
            npu_temp = self._npu_temp_cached
            npu_load = self._npu_load_cached

        # Human-readable "last pruning" age.
        if last_change == 0.0:
            last_prune_str = "never"
        else:
            secs = time.monotonic() - last_change
            if secs < 120:
                last_prune_str = f"{int(secs)}s ago"
            elif secs < 3600:
                last_prune_str = f"{int(secs/60)}m ago"
            else:
                last_prune_str = f"{secs/3600:.1f}h ago"

        # Use background-sampled hardware values when available, with a safe
        # one-shot fallback before the first sample arrives.
        if npu_temp == "N/A" and npu_load == "N/A":
            npu_temp, npu_load = self._read_hwmon_npu()

        since_user_secs = max(0.0, now_mono - last_user)
        since_assistant_secs = max(0.0, now_mono - last_assistant) if last_assistant > 0 else -1.0
        idle_phase_secs = max(0.0, now_mono - idle_phase_start)
        uptime_secs = max(0.0, now_mono - daemon_start)
        last_sample_age_secs = max(0.0, now_mono - last_sample) if last_sample > 0 else -1.0
        quiet_heartbeats = int(since_user_secs // max(1, time_poll_seconds))

        events_10m = sum(1 for ts in exchange_events if now_mono - ts <= 600)
        events_60m = sum(1 for ts in exchange_events if now_mono - ts <= 3600)
        events_6h = sum(1 for ts in exchange_events if now_mono - ts <= 21600)

        exchange_tempo = "insufficient data"
        if len(exchange_events) >= 2:
            recent = exchange_events[-8:]
            gaps = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
            if gaps:
                avg_gap = sum(gaps) / len(gaps)
                exchange_tempo = f"{self._tempo_label(avg_gap)} (~{self._format_elapsed(avg_gap)} avg gap)"

        weekday = time.strftime("%A", now_local)
        day_phase = self._day_phase(now_local.tm_hour)

        return {
            "token_est":      token_est,
            "ctx_total":      self._ctx_size,
            "ctx_pct":        ctx_pct,
            "mem_nodes":      mem_nodes,
            "scratch_nodes":  scratch_nodes,
            "last_prune":     last_prune_str,
            "npu_temp":       npu_temp,
            "npu_load":       npu_load,
            "uptime":         self._format_elapsed(uptime_secs),
            "since_user":     self._format_elapsed(since_user_secs),
            "since_assistant": (
                self._format_elapsed(since_assistant_secs)
                if since_assistant_secs >= 0
                else "never in this daemon session"
            ),
            "idle_phase":     self._format_elapsed(idle_phase_secs),
            "felt_elapsed_wall": self._format_elapsed(felt_elapsed_wall),
            "time_samples":   time_samples,
            "last_sample_age": (
                self._format_elapsed(last_sample_age_secs)
                if last_sample_age_secs >= 0
                else "not sampled yet"
            ),
            "time_poll_seconds": time_poll_seconds,
            "quiet_heartbeats": quiet_heartbeats,
            "events_10m": events_10m,
            "events_60m": events_60m,
            "events_6h": events_6h,
            "exchange_tempo": exchange_tempo,
            "weekday": weekday,
            "day_phase": day_phase,
            "suspend_events": suspend_events,
            "last_suspend_gap": self._format_elapsed(last_suspend_gap),
        }

    # ------------------------------------------------------------------
    # Proactive nudge watcher  (background thread, wakes every 60 s)
    # ------------------------------------------------------------------
    def _watcher_thread(self) -> None:
        log.info("Nudge watcher started")
        while True:
            time.sleep(self._time_poll_seconds)
            try:
                self._sample_temporal_state()
                self._consider_nudge()
                self._consider_slow_wave()
            except Exception as exc:
                log.error("Nudge watcher error: %s", exc)

    def _daily_log_has_entries(self) -> bool:
        """Return True if the daily exchange log exists and has fragments."""
        if not DAILY_LOG_FILE.exists():
            return False
        try:
            data = json.loads(DAILY_LOG_FILE.read_text())
            return isinstance(data, list) and len(data) > 0
        except Exception:
            return False

    def _consider_slow_wave(self) -> None:
        """Nightly slow-wave trigger: 02:00-05:00, idle >= 30m, once/day."""
        with self._lock:
            today = time.strftime("%Y-%m-%d", time.localtime())
            if today != self._slow_wave_last_date:
                self._slow_wave_ran_today = False
                self._slow_wave_last_date = today

            if self._slow_wave_ran_today:
                return

            now_local = time.localtime()
            if now_local.tm_hour < 2 or now_local.tm_hour >= 5:
                return

            idle_secs = time.monotonic() - self._last_user_time
            if idle_secs < 1800:
                return

        if not self._daily_log_has_entries():
            return

        with self._lock:
            if self._slow_wave_ran_today:
                return
            self._slow_wave_ran_today = True

        log.info("Slow-wave trigger passed; launching nightly cycle")
        threading.Thread(
            target=self._slow_wave_thread,
            daemon=True,
            name="bubble-slowwave",
        ).start()

    def _slow_wave_thread(self) -> None:
        """Run dream first, then run the standalone sleep cycle."""
        try:
            self._check_dream(reason="sleep")

            # Preserve ordering from the spec: dream pass first, then slow-wave.
            for _ in range(180):  # up to 15 minutes
                with self._lock:
                    dreaming = self._dreaming
                if not dreaming:
                    break
                time.sleep(5)

            import sleep_cycle

            sleep_cycle.run_sleep_cycle(
                log_file=str(DAILY_LOG_FILE),
                flm_port=self._port,
            )
            log.info("Slow-wave nightly cycle complete")
        except Exception as exc:
            log.error("Slow-wave nightly cycle failed: %s", exc)

    def _consider_nudge(self) -> None:
        with self._lock:
            if not self._nudge_enabled:
                return
            if self._summarizing:
                return
            if self._nudging:
                # Stale-guard: if the nudge thread has been "running" for
                # more than 5 minutes it almost certainly hung silently.
                # Reset the flag so the watcher can try again.
                age = time.monotonic() - self._nudge_start_time
                if age < 300:
                    return
                log.warning(
                    "Nudge thread appears stuck (%.0f s); resetting _nudging", age
                )
                self._nudging = False

            now        = time.monotonic()
            idle_secs  = now - self._last_user_time
            idle_min   = idle_secs / 60.0
            since_last = (now - self._last_nudge_time) / 60.0
            threshold  = float(self._nudge_idle_minutes)
            cooldown   = float(max(self._nudge_cooldown_min, self._nudge_idle_minutes))
            if idle_min < threshold:
                return
            if self._last_nudge_time > 0 and since_last < cooldown:
                return
            # All checks passed — claim the slot.
            self._nudging          = True
            self._nudge_start_time = time.monotonic()

        log.info(
            "Nudge triggered: idle=%.0f min  since_last=%.0f min",
            idle_min, since_last,
        )
        self._set_status("hanging-out", "idle")
        self._fire_nudge(reason="idle", idle_minutes=int(idle_min))

    def _fire_nudge(self, reason: str = "idle", idle_minutes: int = 0) -> None:
        """Gather telemetry, build clock-tick prompt, launch buffered thread."""
        import uuid as _uuid
        token = str(_uuid.uuid4())

        # Stamp start time if not already set (TriggerNudge bypasses the lock).
        with self._lock:
            if self._nudge_start_time == 0.0:
                self._nudge_start_time = time.monotonic()

        import datetime
        now_str = datetime.datetime.now().strftime("%A, %B %-d %Y %H:%M")

        if reason == "manual":
            idle_note = "The user manually requested a clock tick."
        else:
            idle_note = (
                f"The user has been quiet for {idle_minutes} minute"
                f"{'s' if idle_minutes != 1 else ''}."
            )

        # Gather telemetry outside the lock (estimate_tokens acquires it).
        tel = self._gather_telemetry()

        clock_tick_msg = (
            f"[This is a moment. Clock tick — {now_str}.  {idle_note}\n\n"
            f"Orient to immediate reality first: current context, telemetry, and your active working notes.\n"
            f"If you choose to speak, prefer one concrete observation grounded in that context over meta-commentary about silence.\n\n"
            f"Your options — choose any combination, or none:\n"
            f"  • Speak to the user (surface text to the bubble).\n"
            f"  • Stay silent (respond with only memory/note commands, or nothing — no bubble will appear).\n"
            f"  • Use working notes ([[BUBBLE:note:key=...]]) to hold a thread without committing.\n"
            f"  • Dream / compress ([[BUBBLE:remember/forget]] commands; no prose) if context pressure warrants it.\n"
            f"You are not required to speak. The bubble only opens if you produce visible text.\n"
            f"If you do speak, be brief and natural — 1–3 sentences, no greeting formula.]"
        )

        with self._lock:
            system_content = self._build_system_content({}, tel)
            snapshot = [{"role": "system", "content": system_content}]
            snapshot.extend(self._history)
            snapshot.append({"role": "user", "content": clock_tick_msg})

        threading.Thread(
            target=self._clock_tick_thread,
            args=(token, snapshot),
            daemon=True,
            name=f"bubble-tick-{token[:8]}",
        ).start()

    def _clock_tick_thread(self, token: str, snapshot: list) -> None:
        """Non-streaming clock-tick.  Only opens the bubble if text is produced."""
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()
        payload = {
            "model":      model,
            "messages":   snapshot,
            "stream":     False,
            "max_tokens": 300,
        }
        log.info("Clock-tick request  token=%s  msgs=%d", token[:8], len(snapshot))
        try:
            resp = requests.post(url, json=payload, timeout=(10, 90))
            resp.raise_for_status()
            raw_text = (
                resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            )
        except Exception as exc:
            log.error("Clock-tick error  token=%s  %s", token[:8], exc)
            # Don't surface errors for silent ticks — just clean up.
            with self._lock:
                self._nudging         = False
                self._last_nudge_time = time.monotonic()
            return

        # Parse commands inside the lock; get clean display text.
        with self._lock:
            clean_text, mem_changed = self._parse_memory_commands(raw_text)
            if clean_text:
                # Commit to history only when the model actually said something.
                self._history.append({"role": "assistant", "content": clean_text})
                self._trim_history()
                now = time.monotonic()
                self._last_assistant_time = now
                self._record_exchange_event_unlocked(now)
            self._nudging         = False
            self._last_nudge_time = time.monotonic()

        if mem_changed:
            self._save_memory()

        if clean_text:
            # Model chose to speak — open the bubble and deliver the text.
            log.info(
                "Clock-tick has text (%d chars) — surfacing to UI  token=%s",
                len(clean_text), token[:8],
            )
            GLib.idle_add(self.NudgeStart, token)
            GLib.idle_add(self.StreamChunk, token, clean_text)
            GLib.idle_add(self.StreamDone, token)
        else:
            log.info(
                "Clock-tick silent (commands only or empty)  token=%s", token[:8]
            )

    def _trim_history(self) -> None:
        """Drop oldest turns to stay within MAX_HISTORY_TURNS pairs."""
        non_sys = [m for m in self._history if m["role"] != "system"]
        excess  = len(non_sys) - MAX_HISTORY_TURNS * 2
        if excess <= 0:
            return
        dropped = 0
        kept: list[dict] = []
        for m in self._history:
            if m["role"] != "system" and dropped < excess:
                dropped += 1
                continue
            kept.append(m)
        self._history[:] = kept
        log.debug("Trimmed %d message(s) from history", dropped)

    # ------------------------------------------------------------------
    # Summarization  (background, non-streaming, silently updates memory)
    # ------------------------------------------------------------------
    def _check_summarize(self) -> None:
        """Trigger a background summarization if history is large enough."""
        with self._lock:
            if self._summarizing:
                return
            pair_count = sum(1 for m in self._history if m["role"] == "user")
            if pair_count < SUMMARIZE_AT_TURNS:
                return
            # Grab the oldest turns to compress: everything except the
            # SUMMARIZE_KEEP_TURNS most recent pairs.
            keep_msgs = SUMMARIZE_KEEP_TURNS * 2
            to_summarize = list(self._history[:-keep_msgs])
            if not to_summarize:
                return
            self._summarizing = True

        log.info(
            "Summarization triggered: compressing %d messages "
            "(keeping %d recent)",
            len(to_summarize), keep_msgs,
        )
        threading.Thread(
            target=self._summarize_thread,
            args=(to_summarize, keep_msgs),
            daemon=True,
        ).start()

    def _summarize_thread(self, to_summarize: list, keep_msgs: int) -> None:
        """Call the model to compress old turns; splice them out of history."""
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()

        convo = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in to_summarize
        )
        summarize_prompt = (
            "Summarize the following conversation segment concisely. "
            "Focus on key facts, user preferences, decisions made, and important context. "
            "Write in third person. Be brief but complete — this summary replaces the "
            "original messages and must stand alone.\n\n"
            + convo
        )
        payload = {
            "model":    model,
            "messages": [{"role": "user", "content": summarize_prompt}],
            "stream":   False,
        }
        try:
            resp = requests.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            summary = (
                resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            )
            if not summary:
                log.warning("Summarization returned empty response — skipping")
                with self._lock:
                    self._summarizing = False
                return
        except Exception as exc:
            log.error("Summarization failed: %s — skipping", exc)
            with self._lock:
                self._summarizing = False
            return

        # Splice the summarized messages out of the front of history and store
        # the summary as an internal memory entry.
        n = len(to_summarize)
        with self._lock:
            # Only drop if the front still matches what we summarized
            # (guards against ClearHistory() racing with us).
            if self._history[:n] == to_summarize:
                del self._history[:n]
                log.info(
                    "Summarization complete: %d chars → history now %d msgs",
                    len(summary), len(self._history),
                )
            else:
                log.info("Summarization complete but history changed — storing only")
            self._memory["_prior_context"] = summary
            self._summarizing = False
            self._last_memory_change_time = time.monotonic()

        self._save_memory()

    # ------------------------------------------------------------------
    # Dream mode  — silent synthesis pass that rewrites memory
    # ------------------------------------------------------------------
    def _subscribe_sleep_signal(self) -> None:
        """Connect to the system bus PrepareForSleep signal."""
        try:
            from pydbus import SystemBus
            sys_bus = SystemBus()
            login1 = sys_bus.get(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
            )
            login1.PrepareForSleep.connect(self._on_prepare_for_sleep)
            log.info("Subscribed to PrepareForSleep (dream-on-sleep enabled)")
        except Exception as exc:
            log.warning("Could not subscribe to PrepareForSleep: %s", exc)

    def _on_prepare_for_sleep(self, going_to_sleep: bool) -> None:
        """Fired by logind just before suspend and just after resume."""
        if going_to_sleep:
            log.info("System going to sleep — triggering dream pass")
            self._check_dream(reason="sleep")

    def _estimate_tokens(self) -> int:
        """Rough token estimate for the current history (4 chars ≈ 1 token)."""
        with self._lock:
            total_chars = sum(len(m.get("content", "")) for m in self._history)
            mem_chars   = sum(len(v) for v in self._memory.values())
        return (total_chars + mem_chars) // 4

    def _check_dream(self, reason: str = "context-pressure") -> None:
        """Trigger a dream pass if warranted."""
        # Estimate tokens BEFORE acquiring the lock -- _estimate_tokens also
        # acquires self._lock and threading.Lock is not reentrant.
        if reason == "context-pressure":
            tokens    = self._estimate_tokens()
            threshold = int(self._ctx_size * 0.70)
            if tokens < threshold:
                return

        with self._lock:
            if self._dreaming or self._summarizing or self._napping:
                return
            if reason == "context-pressure":
                log.info(
                    "Context pressure dream: ~%d tokens >= 70%% of %d",
                    tokens, self._ctx_size,
                )
            elif reason in ("sleep", "sleep-intent"):
                has_history = bool(self._history)
                has_memory  = any(
                    not k.startswith("_") for k in self._memory
                )
                if not has_history and not has_memory:
                    return
            self._dreaming = True

        threading.Thread(
            target=self._dream_thread,
            args=(reason,),
            daemon=True,
            name=f"bubble-dream-{reason}",
        ).start()

    def _dream_thread(self, reason: str) -> None:
        """Silent synthesis: ask the model to reflect on memory + history,
        emit only [[BUBBLE:remember/forget]] commands.  Nothing is shown to
        the user; the result is piped straight through _parse_memory_commands.
        """
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()

        with self._lock:
            user_mem = {
                k: v for k, v in self._memory.items() if not k.startswith("_")
            }
            prior   = self._memory.get("_prior_context", "")
            history = list(self._history[-20:])  # most recent 20 msgs

        # Build a rich context block for the synthesis prompt.
        mem_block = ""
        if user_mem:
            mem_block = "Current memory:\n" + "\n".join(
                f"  {k}: {v}" for k, v in user_mem.items()
            )
        prior_block = f"Prior summary:\n{prior}" if prior else ""
        history_block = ""
        if history:
            history_block = "Recent conversation:\n" + "\n".join(
                f"  {m['role'].upper()}: {m['content'][:200]}"
                for m in history
            )

        context_str = "\n\n".join(
            b for b in [mem_block, prior_block, history_block] if b
        )

        if reason in ("sleep", "sleep-intent"):
            task = (
                "The system is about to sleep. "
                "Reflect quietly on everything below. "
            )
        else:
            task = (
                "Your context is becoming full. "
                "Reflect on everything below and consolidate. "
            )

        dream_prompt = (
            f"{task}"
            "Find patterns, resolve contradictions, and surface anything worth carrying forward. "
            "Return output in this exact structure:\n"
            "[[BUBBLE:prior_context]]\n"
            "<compressed context summary, 6-12 lines, concrete and concise>\n"
            "[[/BUBBLE:prior_context]]\n"
            "Then zero or more memory commands like [[BUBBLE:remember:key=value]] "
            "or [[BUBBLE:forget:key]].\n"
            "No extra prose outside the block and command lines."
            f"\n\n{context_str}"
        )

        payload = {
            "model":      model,
            "messages":   [{"role": "user", "content": dream_prompt}],
            "stream":     False,
            "max_tokens": 512,
        }
        log.info("Dream pass starting  reason=%s", reason)
        self._set_status("sleeping", "deep consolidation")
        try:
            resp = requests.post(url, json=payload, timeout=(10, 120))
            resp.raise_for_status()
            raw = (
                resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
            )
        except Exception as exc:
            log.error("Dream pass failed: %s", exc)
            with self._lock:
                self._dreaming = False
            return

        prior_summary, remainder = self._extract_prior_context_block(raw)

        # Run the output through the memory command parser.
        # We hold the lock for the parse so memory writes are atomic.
        with self._lock:
            _, mem_changed = self._parse_memory_commands(remainder)
            prior_changed = False
            if prior_summary:
                self._memory["_prior_context"] = prior_summary
                # Keep only a tiny recent tail after dream consolidation.
                if len(self._history) > 6:
                    self._history = self._history[-6:]
                prior_changed = True
                self._last_memory_change_time = time.monotonic()
            self._dreaming = False

        if mem_changed or prior_changed:
            self._save_memory()
            log.info("Dream pass complete (reason=%s) — memory/prior context updated", reason)
        else:
            log.info("Dream pass complete (reason=%s) — no changes", reason)
        self._set_status("hanging-out", "idle")

    def _check_nap(self, reason: str = "model-intent") -> None:
        """Trigger a short nap pass outside the sleep window."""
        with self._lock:
            if self._napping or self._dreaming or self._summarizing:
                return
            self._napping = True

        threading.Thread(
            target=self._nap_thread,
            args=(reason,),
            daemon=True,
            name=f"bubble-nap-{reason}",
        ).start()

    def _nap_thread(self, reason: str) -> None:
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()

        token_est = self._estimate_tokens()
        ctx_pct = int(100 * token_est / self._ctx_size) if self._ctx_size else 0

        with self._lock:
            history_all = list(self._history)
            memory_json = json.dumps(self._memory, ensure_ascii=True)

        # If context is crowded, only expose a recent slice.
        if ctx_pct < 50:
            history_view = history_all
            trunc_note = "full"
        else:
            history_view = history_all[-20:]
            trunc_note = "truncated-recent"

        convo = "\n".join(
            f"{m.get('role', 'unknown').upper()}: {m.get('content', '')[:350]}"
            for m in history_view
        )

        nap_prompt = (
            "You are entering a short nap consolidation cycle. "
            "Review the provided conversation slice and full memory JSON. "
            "Return output in this exact structure:\n"
            "[[BUBBLE:prior_context]]\n"
            "<compressed context summary for future turns, 6-12 lines>\n"
            "[[/BUBBLE:prior_context]]\n"
            "Then optional memory commands (remember/forget/note/unnote/system).\n"
            "No extra prose outside the block and command lines.\n\n"
            f"Nap reason: {reason}\n"
            f"Context pressure: {ctx_pct}% ({trunc_note})\n\n"
            "Conversation slice:\n"
            f"{convo}\n\n"
            "Memory JSON:\n"
            f"{memory_json}"
        )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": nap_prompt}],
            "stream": False,
            "max_tokens": 700,
        }

        log.info("Nap pass starting  reason=%s  ctx_pct=%d", reason, ctx_pct)
        self._set_status("napping", "context review")
        try:
            resp = requests.post(url, json=payload, timeout=(10, 120))
            resp.raise_for_status()
            raw = (
                resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
            )
        except Exception as exc:
            log.error("Nap pass failed: %s", exc)
            with self._lock:
                self._napping = False
            return

        prior_summary, remainder = self._extract_prior_context_block(raw)

        with self._lock:
            _, mem_changed = self._parse_memory_commands(remainder)
            prior_changed = False
            if prior_summary:
                self._memory["_prior_context"] = prior_summary
                # Wake up with compact working context.
                if len(self._history) > 10:
                    self._history = self._history[-10:]
                prior_changed = True
                self._last_memory_change_time = time.monotonic()
            self._napping = False

        if mem_changed or prior_changed:
            self._save_memory()
            log.info("Nap pass complete  reason=%s  updated=%s", reason, True)
        else:
            log.info("Nap pass complete  reason=%s  updated=%s", reason, False)
        self._set_status("hanging-out", "idle")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    _ensure_session_bus_address()
    bus    = SessionBus()
    daemon = BubbleDaemon()
    # Publish at explicit path so the extension proxy finds it at /com/bubble/Daemon
    bus.publish("com.bubble.Daemon", ("/com/bubble/Daemon", daemon))

    loop = GLib.MainLoop()
    log.info("com.bubble.Daemon published on the session bus — waiting")
    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
