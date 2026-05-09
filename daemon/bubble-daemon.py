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
import sys
import json
import threading
import logging

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

# ---------------------------------------------------------------------------
# D-Bus XML introspection
# ---------------------------------------------------------------------------
DBUS_XML = """<node>
  <interface name='com.bubble.Daemon'>

    <method name='SendMessage'>
      <arg type='s' name='message' direction='in'/>
      <arg type='s' name='token'   direction='in'/>
    </method>

    <method name='ClearHistory'/>

    <method name='GetHistory'>
      <arg type='s' name='history_json' direction='out'/>
    </method>

    <method name='Ping'>
      <arg type='s' name='pong' direction='out'/>
    </method>

    <signal name='StreamChunk'>
      <arg type='s' name='token'/>
      <arg type='s' name='text'/>
    </signal>

    <signal name='StreamDone'>
      <arg type='s' name='token'/>
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
    StreamDone  = signal()
    StreamError = signal()

    # ------------------------------------------------------------------
    def __init__(self) -> None:
        self._history: list[dict] = []
        self._lock    = threading.Lock()
        self._port    = _get_port()
        self._model   = os.environ.get("BUBBLE_MODEL", "")
        log.info(
            "Bubble daemon ready  port=%d  model=%s",
            self._port,
            self._model or "(auto-detect)",
        )

    # ------------------------------------------------------------------
    # D-Bus methods
    # ------------------------------------------------------------------
    def Ping(self) -> str:
        return "pong"

    def SendMessage(self, message: str, token: str) -> None:
        """Queue a user message for streaming; token is provided by the caller."""
        with self._lock:
            self._history.append({"role": "user", "content": message})
            snapshot = list(self._history)

        threading.Thread(
            target=self._stream_thread,
            args=(token, snapshot),
            daemon=True,
        ).start()

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

    def _stream_thread(self, token: str, messages: list) -> None:
        url   = f"http://127.0.0.1:{self._port}/v1/chat/completions"
        model = self._auto_model()
        payload = {
            "model":    model,
            "messages": messages,
            "stream":   True,
        }

        full_text = ""
        try:
            with requests.post(
                url, json=payload, stream=True, timeout=120
            ) as resp:
                resp.raise_for_status()
                self._consume_sse(resp, token, full_text)
                return  # _consume_sse handles commit + done/error signals

        except Exception as exc:
            log.error("Stream error (outer)  token=%s  %s", token[:8], exc)
            GLib.idle_add(self.StreamError, token, str(exc))

    def _consume_sse(
        self,
        resp: requests.Response,
        token: str,
        full_text: str,
    ) -> None:
        # answer_text accumulates only `content` chunks (not reasoning_content).
        # This is what gets saved to history so future prompts stay clean.
        # reasoning_content is streamed to the UI for visibility but discarded
        # from the context — sending <think> tokens back to the model causes
        # runaway repetition on the next turn.
        answer_text = ""
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
                    # Answer token — stream to UI and keep for history.
                    answer_text += content
                    GLib.idle_add(self.StreamChunk, token, content)
                elif reasoning:
                    # Reasoning token — stream to UI only, not to history.
                    GLib.idle_add(self.StreamChunk, token, reasoning)

        except ChunkedEncodingError as exc:
            # FastFlowLM closes the connection without a final chunk terminator;
            # treat whatever we collected as the complete response.
            log.info(
                "Stream closed without [DONE] (normal for FastFlowLM)  "
                "token=%s  chars=%d  exc=%s",
                token[:8], len(answer_text), exc,
            )

        # Commit only the clean answer to history.
        with self._lock:
            self._history.append(
                {"role": "assistant", "content": answer_text}
            )
            self._trim_history()

        GLib.idle_add(self.StreamDone, token)
        log.info("Stream done  token=%s  answer_chars=%d", token[:8], len(answer_text))

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
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
