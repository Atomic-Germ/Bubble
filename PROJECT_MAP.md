# Project Map

```
Bubble/
|
+-- daemon/                              # Core daemon — the "brain"
|   +-- bubble-daemon.py                 # 2,600-line daemon (all business logic lives here)
|   +-- bubble.service                   # systemd user service unit
|   +-- requirements.txt                 # Python deps: pydbus, requests
|   +-- __pycache__/                     # Compiled bytecode (gitignored)
|
+-- gnome-extension/bubble@bubble/       # GNOME Shell extension — the "face"
|   +-- extension.js                     # ~1,000-line extension (UI + D-Bus proxy)
|   +-- stylesheet.css                   # Dark-themed styling (window, bubbles, input)
|   +-- prefs.js                         # Settings preferences panel (GTK/Adw)
|   +-- metadata.json                    # Extension manifest (UUID, shell-version)
|   +-- schemas/                         # GSettings schema (XML)
|       +-- org.gnome.shell.extensions.bubble.gschema.xml
|       +-- gschemas.compiled            # Compiled schema (gitignored)
|
+-- tests/                               # pytest suite
|   +-- conftest.py                      # Fixture: creates BubbleDaemon with mocked paths
|   +-- test_memory_protocol.py          # [[BUBBLE:remember|forget|note|system|sleep]] parser
|   +-- test_lifecycle.py                # Backup, scratch, daily log, dream/nap/nudge/slow-wave
|   +-- test_memory_selection.py         # Relevance scoring (lexical, semantic, recency, tags)
|   +-- test_dbus_methods.py             # D-Bus method surface (memory, history, ping)
|   +-- test_supporting_logic.py         # Helpers: cosine similarity, env parsing, elapsed fmt
|
+-- install.sh                           # One-shot install: daemon, systemd, extension, OFLM
+-- update.sh                            # Incremental update: daemon + extension only
+-- .gitlab-ci.yml                       # CI: runs pytest
+-- .gitignore                           # Ignores __pycache__
|
+-- README.md                            # User-facing docs
+-- pi-session-*.html                    # (legacy) Pi session HTML dump
```

## Data flow (user perspective)

```
  User presses Super+B
        |
        v
  GNOME Extension (extension.js)
    - Shows/hides floating St.Bubble
    - D-Bus proxy → com.bubble.Daemon
    - Streams tokens → renders in UI
        |
        v
  Daemon (bubble-daemon.py)
    - Receives message via SendMessage() D-Bus method
    - Builds system prompt (system prompt + self-prompt + memory + context + protocol)
    - Calls FastFlowLM /v1/chat/completions (SSE stream)
    - Parses [[BUBBLE:…]] output → writes to memory.json / scratch.json
    - Emits StreamChunk/StreamThink/StreamDone/StreamError signals back to UI
    - Background thread: nudge, dream, nap, slow-wave, temporal sampling
        |
        v
  FastFlowLM server (external, 127.0.0.1:52625)
    - LLM inference
    - Optional /v1/embeddings for semantic recall
```

## Key architecture facts

- **Single responsibility split**: daemon = brain, extension = face, systemd = supervisor.
- **D-Bus is the only IPC**: all extension ↔ daemon communication goes through `com.bubble.Daemon` on the session bus.
- **Daemon is the source of truth** for history, memory, system prompt, scratch.
- **Memory persistence**: `~/.local/share/bubble/memory.json` (facts) and `~/.local/share/bubble/scratch.json` (working notes).
- **System prompt persistence**: `~/.local/share/bubble/system_prompt.txt` (user-facing).
- **Daily exchange log**: `$XDG_RUNTIME_DIR/bubble_daily.json` (slow-wave input).
- **Streaming**: SSE from FastFlowLM, tokens streamed back to extension via D-Bus signals.
- **Memory protocol**: model outputs `[[BUBBLE:remember:key=value]]` style commands parsed after each response.
- **Self-prompt protocol**: model can propose identity revisions via `[[BUBBLE:system:propose:...]]`, user approves via D-Bus, model commits with `[[BUBBLE:system:commit]]`.
- **Sleep cycle**: model requests `[[BUBBLE:sleep]]` → daemon routes to nap (outside sleep window) or dream (inside sleep window).
- **Slow-wave**: nightly pass at 02:00-05:00 if idle 30m+, synthesizes prior context from daily log.
- **Nudge**: proactive messages after idle threshold, with cooldown.
- **Embeddings**: semantic memory recall via FastFlowLM `/v1/embeddings` endpoint (off/auto/on).
- **History trimming**: max 50 turns; auto-summarizes when history reaches 40 pairs.
