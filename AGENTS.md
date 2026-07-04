# AGENTS.md — Bubble Development Guide

## Quick start

```bash
# Install from source
./install.sh

# Update just daemon + extension
./update.sh

# Run tests
pytest tests/

# Check daemon status
systemctl --user status bubble
journalctl --user -u bubble -f
```

## Project layout

See `PROJECT_MAP.md` for the full map. Key files:

- `daemon/bubble-daemon.py` — everything lives here (2,600 lines). This is the single source of truth.
- `gnome-extension/bubble@bubble/extension.js` — UI + D-Bus proxy (~1,000 lines).
- `tests/conftest.py` — the `daemon` fixture mocks all file paths and GLib.idle_add.
- `tests/test_memory_protocol.py` — parser tests for `[[BUBBLE:...]]` commands.

## Coding conventions

### Daemon (Python)

- **One file, one class**: `bubble-daemon.py` contains a single `BubbleDaemon` class. All methods are instance methods. No standalone functions outside the class.
- **Thread safety**: `self._lock` (threading.Lock) guards `_history`, `_memory`, `_scratch`. Methods that mutate shared state acquire it; read-only methods may skip it.
- **GLib integration**: `GLib.idle_add()` is used to emit D-Bus signals from background threads. Always wrap signal emissions in `GLib.idle_add()`.
- **Environment variables**: use `_get_int_env()` / `_get_float_env()` helpers for validated env reading. Never use raw `os.environ.get()` for config values without validation.
- **File writes**: always atomic (write to `.tmp` then `replace()`). Backup old memory files before overwrite.
- **Constants**: defined at module level (e.g., `MAX_HISTORY_TURNS = 50`). New constants go near the top, grouped with related ones.
- **Logging**: use the module-level `log` (from `logging.getLogger("bubble")`). Format: `log.info("message", arg1, arg2)`.

### Extension (JavaScript)

- **GNOME 45+ ESM imports**: use `import ... from 'gi://...'` and `import ... from 'resource:///...'`.
- **D-Bus signals**: use `g-signal` on Gio.DBusProxy (not individual proxy methods). The handler unpacks params and routes by signal name.
- **Token matching**: always verify `token === this._currentToken` before processing signals — prevents stale stream updates.
- **Markdown → Pango**: `_markdownToPango()` converts Markdown to Pango markup for display.
- **Reconnection**: `_connectDaemon()` is recursive — it calls itself after 1s timeout if daemon isn't ready.

## Testing

- Run with: `pytest tests/` (no special config file — uses defaults).
- The `daemon` fixture (in `conftest.py`) creates a `BubbleDaemon` with all file paths pointing to a temp directory. Monkeypatch `bd.GLib.idle_add` to `lambda *a, **k: None` to avoid GLib main loop errors.
- Tests import the daemon via `importlib.util.spec_from_file_location` because `bubble-daemon.py` has a hyphen in its name (invalid module name).
- **Test patterns**: use class-based tests with descriptive method names (e.g., `test_backup_created_when_different`).

## Key patterns to know

### D-Bus contract

All methods/signals defined in both files:
- `daemon/bubble-daemon.py` line ~120 (DBUS_XML constant)
- `gnome-extension/bubble@bubble/extension.js` line ~19 (DBUS_IFACE constant)

When adding a D-Bus method, update BOTH. They must match.

### Memory protocol (`[[BUBBLE:...]]`)

The daemon parses these from model output after each response:

| Command | Purpose | Example |
|---------|---------|---------|
| `remember` | Store a fact | `[[BUBBLE:remember:self:key=value]]` |
| `forget` | Delete a fact | `[[BUBBLE:forget:key]]` |
| `note` | Scratchpad entry | `[[BUBBLE:note:key=text]]` |
| `unnote` | Remove scratchpad | `[[BUBBLE:unnote:key]]` or `[[BUBBLE:unnote:*]]` |
| `system` | Self-prompt proposal/commit | `[[BUBBLE:system:propose:...]]` / `[[BUBBLE:system:commit]]` |
| `sleep` | Request rest | `[[BUBBLE:sleep:nap]]` / `[[BUBBLE:sleep:sleep]]` / `[[BUBBLE:sleep]]` |
| `prior_context` | Synthesized conversation summary | `[[BUBBLE:prior_context]]...[[/BUBBLE:prior_context]]` |

Key regex patterns (daemon, line ~707-738): `_RE_REMEMBER`, `_RE_FORGET`, `_RE_NOTE`, `_RE_UNNOTE`, `_RE_SYSTEM`, `_RE_SLEEP`, `_RE_PRIOR_BLOCK`.

Placeholder detection (`_is_placeholder()`) prevents executing example text like `key=value` or `...` as real commands.

### Streaming flow

1. Extension calls `SendMessage(message, token, context_json)` via D-Bus.
2. Daemon builds system snapshot, POSTs to FastFlowLM `/v1/chat/completions` with `stream: true`.
3. Daemon consumes SSE in `_consume_sse()`, emitting `StreamChunk` / `StreamThink` / `StreamEdit` signals.
4. On `StreamDone`: daemon parses `[[BUBBLE:...]]` commands, saves to memory, appends assistant response to history.
5. Daemon checks for sleep intent → routes to nap/dream.
6. Extension receives signals, renders tokens, formats markdown on completion.

### Background threads

The daemon runs these background workers:

- `_watcher_thread()`: periodic nudge, dream, nap, slow-wave, temporal sampling checks.
- `_stream_thread()`: launched per user message for streaming.
- `_dream_thread()`: "dream" pass — sends summary to model for consolidation.
- `_nap_thread()`: "nap" pass — lightweight context review.
- `_slow_wave_thread()`: nightly pass (02:00-05:00) synthesizes prior context from daily log.

Flap guards: `self._dreaming`, `self._napping`, `self._summarizing`, `self._dreaming` prevent concurrent runs.

### Memory selection

When building the system prompt, `_select_relevant_memory()` ranks memory items by:
1. Lexical match (key/val terms vs query terms) — 4pts key match, 2pts val match
2. Intent/topic tags — 6pts/4pts bonus
3. Sentiment strength — up to 4pts
4. User identity — 2pts
5. Recency — up to 8pts (decreases with age)
6. Semantic similarity (cosine) — up to 6pts bonus
7. Index tiebreaker (recency)

Only items with score > 0 are shown (falling back to recency ordering if all zero).

### Self-prompt protocol

The model can propose identity revisions:
1. Model outputs `[[BUBBLE:system:propose:revised text]]` → stored as pending
2. Extension shows proposal pill with agree/reject buttons
3. User approves via D-Bus `SetSelfPromptProposalApproval(true)`
4. Model commits with `[[BUBBLE:system:commit]]` → moves pending to `_self_prompt`
5. `_self_prompt` is injected into system prompt below user prompt, above memory

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FLM_SERVE_PORT` | `52625` | FastFlowLM server port |
| `BUBBLE_MODEL` | auto-detect | Specific model name |
| `BUBBLE_SYSTEM_PROMPT` | file | System prompt (env takes precedence over file) |
| `BUBBLE_CTX` | `8192` | Estimated context window size (for dream trigger) |
| `BUBBLE_EMBEDDINGS_ENABLED` | `auto` | off/auto/on |
| `BUBBLE_EMBED_MODEL` | `embed-gemma:300m` | Embedding model name |
| `BUBBLE_EMBED_TIMEOUT` | `3.0` | Embedding API timeout (seconds) |
| `BUBBLE_EMBED_MAX_TEXT_CHARS` | `1600` | Max text length for embeddings |
| `BUBBLE_MEMORY_BACKUP_LIMIT` | `50` | Max memory backup files to retain |
| `BUBBLE_TIME_POLL_SECONDS` | `15` | Temporal sampling interval |

## GSettings keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bubble-toggle` | `as` | `['<Super>b']` | Keyboard shortcut |
| `system-prompt` | `s` | `''` | System prompt (pushed to daemon) |
| `nudge-enabled` | `b` | `true` | Enable proactive nudges |
| `nudge-idle-minutes` | `i` | `30` | Idle threshold for nudges |

## D-Bus methods (summary)

### Send methods
- `SendMessage(message, token, context_json)` — user message
- `SetSystemPrompt(prompt)` — update system prompt
- `SetNudgeConfig(enabled, idle_minutes)` — push nudge settings
- `TriggerNudge()` — manual nudge (testing)
- `WriteMemory(key, value)` — remember a fact
- `SetSelfPromptProposalApproval(approved)` — approve/reject self-prompt

### Query methods
- `GetSystemPrompt()` → prompt text
- `GetHistory()` → JSON history
- `ReadMemory(key)` → value
- `GetMemory()` → JSON memory
- `GetSelfPromptProposal()` → (proposal, approved)
- `Ping()` → "pong"

### State methods
- `ClearHistory()` — clear conversation history
- `ClearMemory()` — clear all memory

### Signals
- `StreamChunk(token, text)` — streamed token
- `StreamThink(token, text)` — reasoning token
- `StreamEdit(token, text)` — edited response
- `StreamDone(token)` — stream complete
- `StreamError(token, error_msg)` — stream error
- `NudgeStart(token)` — daemon initiating unsolicited message
- `StatusChanged(state, detail)` — daemon status
- `SelfPromptProposalChanged(has_pending, approved)` — proposal state

## File paths (daemon runtime)

| File | Purpose |
|------|---------|
| `~/.local/share/bubble/memory.json` | Persistent facts |
| `~/.local/share/bubble/memory-backups/` | Timestamped memory backups |
| `~/.local/share/bubble/scratch.json` | Working notes (scratchpad) |
| `~/.local/share/bubble/system_prompt.txt` | Persisted system prompt |
| `$XDG_RUNTIME_DIR/bubble_daily.json` | Daily exchange log (slow-wave input) |

## Common gotchas

1. **D-Bus XML must match**: `DBUS_XML` in daemon and `DBUS_IFACE` in extension must have the same methods/signals. Mismatch causes silent failures.
2. **Token matching**: always check `token === this._currentToken` in extension signal handlers. Without it, stale stream data from previous messages renders over current ones.
3. **Daemon restart clears memory**: no — memory persists across restarts (loaded from disk). History clears on restart.
4. **pydbus signal emission**: use `GLib.idle_add(self.SignalName, ...)` from background threads, not direct calls.
5. **Stream watchdog**: extension has a stream watchdog timer — if no tokens arrive within timeout, the stream is considered stuck.
6. **Memory protocol parsing happens AFTER the response is complete**: commands are extracted from the full accumulated text, not incrementally.
7. **Internal memory keys (prefixed `_`) are protected**: model output cannot `forget` them.
8. **`_prior_context` is auto-managed**: auto-summarization writes it; model cannot read/write it directly.
