# Bubble

A minimal resident AI chat assistant for GNOME, powered by [FastFlowLM](https://fastflowlm.com/).

Press **Super+B** anywhere on your desktop to open a floating chat bubble.  
The conversation context is maintained continuously until cleared.

---

## Architecture

```
┌────────────────────────────────────────────┐
│  GNOME Shell Extension  (bubble@bubble)    │
│  • Global keybinding  (Super+B)            │
│  • Floating St panel with streaming UI     │
│  • Communicates via D-Bus                  │
└────────────────────┬───────────────────────┘
                     │ D-Bus  (com.bubble.Daemon)
┌────────────────────▼───────────────────────┐
│  Bubble Daemon  (systemd user service)     │
│  • Holds conversation history              │
│  • Streams from FastFlowLM  /v1/chat/…    │
│  • Emits StreamChunk / StreamDone signals  │
└────────────────────┬───────────────────────┘
                     │ HTTP SSE (port 52625)
┌────────────────────▼───────────────────────┐
│  FastFlowLM server  (pre-existing)         │
└────────────────────────────────────────────┘
```

## Requirements

- GNOME Shell 45+
- Python 3.10+
- `pydbus` and `requests` Python packages
- FastFlowLM server running on `127.0.0.1:52625`  
  (override with `$FLM_SERVE_PORT` environment variable)

## Installation

```bash
chmod +x install.sh
./install.sh
```

Then:

1. Restart GNOME Shell  
   - **X11**: `Alt+F2` → type `r` → Enter  
   - **Wayland**: log out and back in
2. Enable the extension:  
   ```bash
   gnome-extensions enable bubble@bubble
   ```

## Usage

| Action | How |
|---|---|
| Open / close bubble | **Super+B** |
| Send message | **Enter** or click **↑** |
| Close bubble | **Escape** or click **✕** |
| Clear history | Click **↺** |

## Configuration

**Change the keyboard shortcut:**
```bash
gsettings set org.gnome.shell.extensions.bubble bubble-toggle "['<Super>b']"
```

**Use a specific model:**
```bash
systemctl --user edit bubble
# Add:  Environment=BUBBLE_MODEL=llama3.2:1b
systemctl --user restart bubble
```

**Optional semantic recall with embeddings:**

Bubble can augment prompts with semantically similar prior turns when an
OpenAI-compatible `/v1/embeddings` endpoint is available.

FastFlowLM example (runs embedding model alongside the LLM):
```bash
flm serve gemma3:4b --embed 1
```

Enable/disable behavior in Bubble service overrides:
```bash
systemctl --user edit bubble
# Add (example):
# [Service]
# Environment=BUBBLE_EMBEDDINGS_ENABLED=auto
# Environment=BUBBLE_EMBED_MODEL=embed-gemma:300m
# Environment=BUBBLE_EMBED_MAX_RECALL=4
systemctl --user restart bubble
```

Modes:
- `off`: never call embeddings endpoint
- `auto`: use embeddings only when endpoint is reachable
- `on`: require embeddings (logs warning + falls back gracefully if unavailable)

**Override the FastFlowLM port:**
```bash
# In ~/.config/systemd/user/bubble.service.d/override.conf
[Service]
Environment=FLM_SERVE_PORT=11434
```

## Daemon commands

```bash
systemctl --user status  bubble    # check status
systemctl --user restart bubble    # restart
journalctl --user -u bubble -f     # live logs
```

## Context window

The daemon keeps up to **50 user+assistant turns** in memory.  
Older turns are trimmed automatically from the top of the history.  
Click **↺** (or call `ClearHistory` via D-Bus) to wipe it manually.
