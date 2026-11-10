// extension.js — Bubble GNOME Shell Extension (GNOME 45+, ESM)

import GLib     from 'gi://GLib';
import Gio      from 'gi://Gio';
import St       from 'gi://St';
import Clutter  from 'gi://Clutter';
import Pango    from 'gi://Pango';
import Meta     from 'gi://Meta';
import Shell    from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main    from 'resource:///org/gnome/shell/ui/main.js';

const SENDMESSAGE_TIMEOUT_MS = 30000;

// ---------------------------------------------------------------------------
// D-Bus interface
// ---------------------------------------------------------------------------
const DBUS_IFACE = `<node>
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
    <method name='SendSidecarMessage'>
      <arg type='s' name='message' direction='in'/>
      <arg type='s' name='token' direction='in'/>
    </method>
    <method name='SetSidecarSummary'>
      <arg type='s' name='summary' direction='in'/>
    </method>
    <method name='CheckSidecarAvailable'>
      <arg type='b' name='available' direction='out'/>
    </method>
    <method name='SummarizeForSidecar'>
      <arg type='s' name='summary' direction='out'/>
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
        <signal name='SidecarSummaryChanged'>
            <arg type='s' name='summary' direction='in'/>
        </signal>
        <signal name='SidecarAvailable'>
            <arg type='b' name='available' direction='in'/>
        </signal>
        <signal name='SidecarError'>
            <arg type='s' name='error' direction='in'/>
        </signal>
        <signal name='SidecarDone'>
            <arg type='s' name='token' direction='in'/>
        </signal>
    <signal name='StreamError'>
      <arg type='s' name='token'/>
      <arg type='s' name='error_msg'/>
    </signal>
  </interface>
</node>`;

// Interface info is not needed for proxy.call() + g-signal; pass null below.

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
// St.BoxLayout historically took a boolean `vertical` property. Newer Shell
// versions replaced it with `orientation` (Clutter.Orientation) and dropped
// `vertical` entirely. Detect which API this runtime provides so the extension
// keeps working across the supported Shell range.
const _BOX_HAS_ORIENTATION = Object.prototype.hasOwnProperty.call(
    St.BoxLayout.prototype, 'orientation'
);

function _boxLayout(props = {}) {
    if (_BOX_HAS_ORIENTATION && props.vertical !== undefined) {
        const boxProps = Object.assign({}, props);
        delete boxProps.vertical;
        boxProps.orientation = props.vertical
            ? Clutter.Orientation.VERTICAL
            : Clutter.Orientation.HORIZONTAL;
        return new St.BoxLayout(boxProps);
    }
    return new St.BoxLayout(props);
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------
export default class BubbleExtension extends Extension {

    enable() {
        console.log('[Bubble] extension.js build 2026-05-24T20:48 tooltipless');
        this._proxy           = null;
        this._proxySigs       = [];
        this._settingsChanged = null;
        this._window          = null;
        this._streaming       = false;
        this._currentToken    = null;
        this._streamLabel     = null;
        this._thinkLabel      = null;
        this._thinkToggle     = null;
        this._statusLabel     = null;
        this._statusState     = 'hanging-out';
        this._proposalLabel   = null;
        this._proposalAgreeBtn = null;
        this._proposalRejectBtn = null;
        this._streamWatchdogId = 0;
        this._lastDaemonStartAttemptMs = 0;
        this._sidecarLabel    = null;
        this._sidecarWatchdogId = 0;
        this._historyRestored = false;

        this._buildUI();
        this._connectDaemon();
        this._registerKeybinding();
        this._checkSidecar();
    }

    disable() {
        // Keybinding
        Main.wm.removeKeybinding('bubble-toggle');

        // UI
        if (this._window) {
            Main.layoutManager.removeChrome(this._window);
            this._window.destroy();
            this._window = null;
        }

        // D-Bus
        for (const id of this._proxySigs)
            this._proxy?.disconnect(id);
        this._proxySigs    = [];
        this._proxy        = null;
        this._streaming    = false;
        this._currentToken = null;
        this._streamLabel  = null;
        this._thinkLabel   = null;
        this._thinkToggle  = null;
        this._statusLabel  = null;
        this._statusState  = 'hanging-out';
        this._proposalLabel = null;
        this._proposalAgreeBtn = null;
        this._proposalRejectBtn = null;
        this._clearStreamWatchdog();


        // Settings
        if (this._settingsChanged) {
            this.getSettings().disconnect(this._settingsChanged);
            this._settingsChanged = null;
        }
    }

    // -----------------------------------------------------------------------
    // D-Bus
    // -----------------------------------------------------------------------
    _connectDaemon() {
        // Reconnecting: drop existing proxy signal handlers first.
        for (const id of this._proxySigs)
            this._proxy?.disconnect(id);
        this._proxySigs = [];
        this._proxy = null;

        Gio.DBusProxy.new(
            Gio.DBus.session,
            Gio.DBusProxyFlags.NONE,
            null,                    // no interface info needed for call()+g-signal
            'com.bubble.Daemon',
            '/com/bubble/Daemon',
            'com.bubble.Daemon',
            null,                    // cancellable
            (source, result) => {
                try {
                    this._proxy = Gio.DBusProxy.new_finish(result);
                } catch (e) {
                    console.error(`[Bubble] D-Bus proxy error: ${e}`);
                    return;
                }

                // Raw Gio.DBusProxy exposes ALL D-Bus signals through the
                // single 'g-signal' GObject signal — not individual methods.
                this._proxySigs.push(
                    this._proxy.connect('g-signal', (_proxy, _sender, signalName, params) => {
                        const unpacked = params.deepUnpack();
                        this._handleDaemonSignal(signalName, unpacked);
                    }),
                );

                console.log('[Bubble] D-Bus proxy connected.');

                if (!this._daemonHasOwner()) {
                    console.warn('[Bubble] Proxy connected without daemon owner; attempting daemon start');
                    this._setStatus('hanging-out', 'daemon starting');
                    this._attemptStartDaemon();
                    GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                        this._connectDaemon();
                        return GLib.SOURCE_REMOVE;
                    });
                    return;
                }

                // Avoid clobbering daemon-side prompt with empty settings on startup.
                this._pushSystemPrompt(false);
                this._pushNudgeConfig();
                this._refreshProposalState();
                if (!this._historyRestored) {
                    this._restoreHistory();
                }
                this._setStatus('hanging-out', 'idle');
                this._settingsChanged = this.getSettings().connect(
                    'changed',
                    (settings, key) => {
                        if (key === 'system-prompt')    this._pushSystemPrompt(true);
                        if (key === 'nudge-enabled' || key === 'nudge-idle-minutes')
                            this._pushNudgeConfig();
                    },
                );
            },
        );
    }

    _pushNudgeConfig() {
        if (!this._proxy) return;
        const settings     = this.getSettings();
        const enabled      = settings.get_boolean('nudge-enabled');
        const idleMinutes  = settings.get_int('nudge-idle-minutes');
        this._proxy.call(
            'SetNudgeConfig',
            new GLib.Variant('(bi)', [enabled, idleMinutes]),
            Gio.DBusCallFlags.NONE,
            2000,
            null,
            null,
        );
    }

    _pushSystemPrompt(force = false) {
        if (!this._proxy) return;
        const prompt = this.getSettings().get_string('system-prompt');
        if (!force && !prompt.trim()) {
            console.log('[Bubble] System prompt settings empty; preserving daemon prompt');
            return;
        }
        this._proxy.call(
            'SetSystemPrompt',
            new GLib.Variant('(s)', [prompt]),
            Gio.DBusCallFlags.NONE,
            2000,
            null,
            null,
        );
        console.log(`[Bubble] System prompt pushed (${prompt.length} chars)`);
    }

    _refreshProposalState() {
        if (!this._proxy) return;
        this._proxy.call(
            'GetSelfPromptProposal',
            null,
            Gio.DBusCallFlags.NONE,
            2000,
            null,
            (proxy, result) => {
                try {
                    const res = proxy.call_finish(result);
                    const [proposal, approved] = res.deepUnpack();
                    this._setProposalState(Boolean((proposal ?? '').trim()), Boolean(approved));
                } catch (e) {
                    const msg = String(e?.message ?? e ?? '');
                    if (this._isDaemonUnavailableError(msg)) {
                        console.warn(`[Bubble] GetSelfPromptProposal daemon unavailable (${msg})`);
                        this._attemptStartDaemon();
                        this._connectDaemon();
                        return;
                    }
                    console.error(`[Bubble] GetSelfPromptProposal error: ${e}`);
                }
            },
        );
    }

    _daemonHasOwner() {
        try {
            const owner = this._proxy?.get_name_owner?.() ?? '';
            return Boolean(String(owner).trim());
        } catch (_e) {
            return false;
        }
    }

    _attemptStartDaemon() {
        const now = Date.now();
        if (now - this._lastDaemonStartAttemptMs < 5000)
            return;
        this._lastDaemonStartAttemptMs = now;
        try {
            const proc = Gio.Subprocess.new(
                ['systemctl', '--user', 'start', 'bubble'],
                Gio.SubprocessFlags.NONE,
            );
            proc.wait_async(null, null);
        } catch (e) {
            console.warn(`[Bubble] Could not start daemon via systemctl: ${e}`);
        }
    }

    _isDaemonUnavailableError(msg) {
        const s = String(msg ?? '').toLowerCase();
        return (
            s.includes('without an owner')
            || s.includes('serviceunknown')
            || s.includes('not activatable')
            || (s.includes('com.bubble.daemon') && s.includes('error.serviceunknown'))
        );
    }

    _isActiveStreamToken(token) {
        return Boolean(token) && token === this._currentToken;
    }

    _resetStreamingState() {
        this._streaming = false;
        this._currentToken = null;
        this._streamLabel = null;
        this._thinkLabel = null;
        this._thinkToggle = null;
    }

    _finishStreamWithError(displayText, statusDetail = 'idle') {
        this._clearStreamWatchdog();
        if (this._streamLabel) {
            this._streamLabel.add_style_class_name('bubble-error');
            this._streamLabel.set_text(displayText);
        }
        this._resetStreamingState();
        this._setInputEnabled(true);
        this._setStatus('hanging-out', statusDetail);
    }

    _handleStreamDone(token) {
        if (!this._isActiveStreamToken(token))
            return;

        this._clearStreamWatchdog();
        // Format the completed answer with markdown → Pango
        // while we still hold a reference to the label.
        if (this._streamLabel) {
            const finalText = this._streamLabel.get_text().trim();
            if (!finalText) {
                // Command-only replies can resolve to empty text.
                // Remove the placeholder assistant bubble entirely.
                const col = this._streamLabel.get_parent();
                const row = col?.get_parent();
                row?.destroy();
            } else {
                const markup = this._markdownToPango(finalText);
                try {
                    this._streamLabel.clutter_text.set_markup(markup);
                } catch (_e) {
                    // Invalid markup — plain text already displayed.
                }
            }
        }
        this._resetStreamingState();
        this._setInputEnabled(true);
    }

    _handleDaemonSignal(signalName, unpacked) {
        const token = unpacked[0];

        if (signalName === 'StreamChunk') {
            const text = unpacked[1];
            if (!this._isActiveStreamToken(token)) return;
            this._bumpStreamWatchdog(token);
            const cur = this._streamLabel?.get_text() ?? '';
            this._streamLabel?.set_text(cur + text);
            this._scrollToBottom();
            return;
        }

        if (signalName === 'StreamThink') {
            const text = unpacked[1];
            if (!this._isActiveStreamToken(token)) return;
            this._bumpStreamWatchdog(token);
            // Reveal the think section on the first token.
            if (this._thinkLabel && !this._thinkLabel.visible) {
                this._thinkLabel.show();
                this._thinkToggle?.show();
            }
            const cur = this._thinkLabel?.get_text() ?? '';
            this._thinkLabel?.set_text(cur + text);
            this._scrollToBottom();
            return;
        }

        if (signalName === 'StreamDone') {
            this._handleStreamDone(token);
            return;
        }

        if (signalName === 'NudgeStart') {
            // Daemon is about to stream an unprompted message.
            // Show the bubble and create an empty assistant slot.
            if (this._streaming) return; // already busy
            this._streaming = true;
            this._currentToken = token;
            this._showBubble();
            try {
                this._streamLabel = this._addMessage('assistant', '');
            } catch (e) {
                console.error(`[Bubble] NudgeStart UI error: ${e}`);
            }
            this._setInputEnabled(false);
            this._startStreamWatchdog(token);
            return;
        }

        if (signalName === 'StatusChanged') {
            const state = unpacked[0];
            const detail = unpacked[1] ?? '';
            this._setStatus(state, detail);
            return;
        }

        if (signalName === 'SelfPromptProposalChanged') {
            const hasPending = unpacked[0];
            const approved = unpacked[1];
            this._setProposalState(hasPending, approved);
            return;
        }

        if (signalName === 'StreamEdit') {
            const cleanText = unpacked[1];
            if (!this._isActiveStreamToken(token)) return;
            this._streamLabel?.set_text(cleanText);
            return;
        }

        if (signalName === 'StreamError') {
            const errMsg = unpacked[1];
            if (!this._isActiveStreamToken(token)) return;
            this._finishStreamWithError(`⚠ ${errMsg}`);
        }
    }

    _recoverFromDaemonUnavailable(token, msg) {
        console.warn(`[Bubble] Daemon unavailable for ${token}; reconnecting (${msg})`);
        this._finishStreamWithError('⚠ Daemon unavailable. Reconnecting…', 'daemon reconnecting');

        this._attemptStartDaemon();

        this._connectDaemon();
    }

    // -----------------------------------------------------------------------
    // UI construction
    // -----------------------------------------------------------------------
    _buildUI() {
        // ── Root container ────────────────────────────────────────────────
        this._window = _boxLayout({
            style_class: 'bubble-window',
            vertical:    true,
            width:       480,
            height:      580,
            visible:     false,
            reactive:    true,
            can_focus:   true,
            opacity:     0,
        });

        // ── Header ────────────────────────────────────────────────────────
        const header = _boxLayout({
            style_class: 'bubble-header',
            vertical:    false,
        });

        const title = new St.Label({
            style_class: 'bubble-title',
            text:        '⬡  Bubble',
            y_align:     Clutter.ActorAlign.CENTER,
            x_expand:    true,
        });

        const statusPill = _boxLayout({
            style_class: 'bubble-status',
            vertical:    false,
            x_align:     Clutter.ActorAlign.END,
            y_align:     Clutter.ActorAlign.CENTER,
        });

        const statusDot = new St.Label({
            style_class: 'bubble-status-dot',
            text:        '•',
        });

        this._statusLabel = new St.Label({
            style_class: 'bubble-status-label',
            text:        'Hanging out',
        });

        statusPill.add_child(statusDot);
        statusPill.add_child(this._statusLabel);

        const proposalPill = _boxLayout({
            style_class: 'bubble-proposal',
            vertical:    false,
            x_align:     Clutter.ActorAlign.END,
            y_align:     Clutter.ActorAlign.CENTER,
            visible:     false,
        });

        this._proposalLabel = new St.Label({
            style_class: 'bubble-proposal-label',
            text:        'Self draft pending',
        });
        proposalPill.add_child(this._proposalLabel);

        this._proposalAgreeBtn = new St.Button({
            style_class: 'bubble-icon-btn bubble-proposal-btn',
            label:       '✓',
            can_focus:   false,
            visible:     false,
        });
        this._proposalAgreeBtn.connect('clicked', () => {
            this._proxy?.call(
                'SetSelfPromptProposalApproval',
                new GLib.Variant('(b)', [true]),
                Gio.DBusCallFlags.NONE,
                2000,
                null,
                null,
            );
        });

        this._proposalRejectBtn = new St.Button({
            style_class: 'bubble-icon-btn bubble-proposal-btn',
            label:       '✕',
            can_focus:   false,
            visible:     false,
        });
        this._proposalRejectBtn.connect('clicked', () => {
            this._proxy?.call(
                'ClearSelfPromptProposal',
                null,
                Gio.DBusCallFlags.NONE,
                2000,
                null,
                null,
            );
        });

        const clearBtn = new St.Button({
            style_class: 'bubble-icon-btn',
            label:       '↺',
            can_focus:   false,
        });
        clearBtn.connect('clicked', () => this._clearHistory());

        const closeBtn = new St.Button({
            style_class: 'bubble-icon-btn',
            label:       '✕',
            can_focus:   false,
        });
        closeBtn.connect('clicked', () => this._hideBubble());

        header.add_child(title);
        header.add_child(statusPill);
        header.add_child(proposalPill);
        header.add_child(this._proposalAgreeBtn);
        header.add_child(this._proposalRejectBtn);
        header.add_child(clearBtn);
        header.add_child(closeBtn);

        // ── Message scroll area ───────────────────────────────────────────
        this._scrollView = new St.ScrollView({
            style_class:          'bubble-scroll',
            vscrollbar_policy:    St.PolicyType.AUTOMATIC,
            hscrollbar_policy:    St.PolicyType.NEVER,
            x_expand:             true,
            y_expand:             true,
            overlay_scrollbars:   true,
        });

        this._msgBox = _boxLayout({
            style_class: 'bubble-msgs',
            vertical:    true,
            x_expand:    true,
        });
        this._scrollView.set_child(this._msgBox);

        // ── Input bar ─────────────────────────────────────────────────────
        const inputRow = _boxLayout({
            style_class: 'bubble-input-row',
            vertical:    false,
        });

        this._entry = new St.Entry({
            style_class: 'bubble-entry',
            hint_text:   'Message…',
            x_expand:    true,
            can_focus:   true,
        });
        this._entry.clutter_text.connect('activate', () => this._send());
        this._entry.clutter_text.connect('key-press-event', (_a, event) => {
            if (event.get_key_symbol() === Clutter.KEY_Escape) {
                this._hideBubble();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        this._sendBtn = new St.Button({
            style_class: 'bubble-send-btn',
            label:       '↑',
            can_focus:   false,
        });
        this._sendBtn.connect('clicked', () => this._send());

        inputRow.add_child(this._entry);
        inputRow.add_child(this._sendBtn);

        // Assemble
        this._window.add_child(header);
        this._window.add_child(this._scrollView);
        this._window.add_child(inputRow);

        Main.layoutManager.addTopChrome(this._window);
        this._positionWindow();
    }

    // -----------------------------------------------------------------------
    // Keybinding — registered for Shell.ActionMode.ALL so it fires even when
    // the bubble's modal grab is active (SYSTEM_MODAL mode).
    // -----------------------------------------------------------------------
    _registerKeybinding() {
        Main.wm.addKeybinding(
            'bubble-toggle',
            this.getSettings(),
            Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.ALL,
            () => this._toggleBubble(),
        );
    }

    // -----------------------------------------------------------------------
    // Show / Hide
    // -----------------------------------------------------------------------
    _toggleBubble() {
        if (this._window.visible)
            this._hideBubble();
        else
            this._showBubble();
    }

    _showBubble() {
        this._positionWindow();
        this._window.show();
        this._window.ease({
            opacity:  255,
            duration: 160,
            mode:     Clutter.AnimationMode.EASE_OUT_QUAD,
        });

        // Route keyboard to the entry using the correct St API —
        // grab_key_focus() is scoped to the Clutter scene graph and does NOT
        // create a compositor-level Wayland keyboard grab.
        GLib.idle_add(GLib.PRIORITY_LOW, () => {
            this._entry.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _hideBubble() {
        this._window.ease({
            opacity:    0,
            duration:   160,
            mode:       Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => this._window.hide(),
        });
    }

    _positionWindow() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor) return;
        const x = monitor.x + monitor.width  - 496;
        const y = monitor.y + Math.floor((monitor.height - 600) / 2);
        this._window.set_position(x, y);
    }

    // -----------------------------------------------------------------------
    // Messaging
    // -----------------------------------------------------------------------
    _send() {
        if (this._streaming || !this._proxy) return;

        if (!this._daemonHasOwner()) {
            this._attemptStartDaemon();
            this._setStatus('hanging-out', 'daemon starting');
            this._addMessage('assistant', '⚠ Daemon is starting. Try again in a moment.');
            return;
        }

        const text = this._entry.get_text().trim();
        if (!text) return;

        this._entry.set_text('');
        this._setInputEnabled(false);
        this._streaming = true;

        // Generate UUID synchronously before the D-Bus call so _currentToken
        // is already set when the first StreamChunk signal arrives.
        const token = GLib.uuid_string_random();
        this._currentToken = token;
        this._startStreamWatchdog(token);

        // Add UI bubbles — kept separate from the proxy call so a UI error
        // can never prevent the message from reaching the daemon.
        try {
            this._addMessage('user', text);
            this._streamLabel = this._addMessage('assistant', '');
            this._scrollToBottom();
        } catch (e) {
            console.error(`[Bubble] UI error in _send: ${e}`);
        }

        this._proxy.call(
            'SendMessage',
            new GLib.Variant('(sss)', [text, token, this._buildContext()]),
            Gio.DBusCallFlags.NONE,
            SENDMESSAGE_TIMEOUT_MS,
            null,
            (_proxy, result) => {
                try {
                    _proxy.call_finish(result);
                } catch (e) {
                    const msg = String(e?.message ?? e ?? '');
                    if (!this._daemonHasOwner() || this._isDaemonUnavailableError(msg)) {
                        this._recoverFromDaemonUnavailable(token, msg);
                        return;
                    }
                    // While this request token is still active, treat callback
                    // errors as non-fatal and keep waiting for stream signals.
                    // D-Bus ack timeouts and transient bus delays can occur even
                    // when the daemon later delivers chunks.
                    if (this._streaming && this._currentToken === token) {
                        console.warn(`[Bubble] SendMessage ack issue for ${token}; awaiting stream signals (${msg})`);
                        this._setStatus('chatting', 'waiting for daemon');
                        if (this._streamLabel && !this._streamLabel.get_text().trim()) {
                            this._streamLabel.set_text('…waiting for daemon');
                        }
                        return;
                    }
                    console.error(`[Bubble] SendMessage error: ${e}`);
                    if (this._streamLabel) {
                        this._streamLabel.add_style_class_name('bubble-error');
                        this._streamLabel.set_text(`⚠ ${e.message}`);
                        this._streamLabel = null;
                    }
                    this._streaming    = false;
                    this._currentToken = null;
                    this._setInputEnabled(true);
                }
            },
        );
    }

    _buildContext() {
        // Snapshot current context synchronously — global.display.focus_window
        // returns the last Meta.Window with focus (a real app window), even
        // while the bubble's Clutter widget holds keyboard focus.
        const ctx = {};

        const now = GLib.DateTime.new_now_local();
        if (now)
            ctx.datetime = now.format('%A, %B %-d %Y %H:%M');

        try {
            const win = global.display.focus_window;
            if (win) {
                const title = win.get_title();
                if (title) ctx.window = title;
            }
        } catch (_e) {}

        return JSON.stringify(ctx);
    }

    _restoreHistory() {
        if (!this._proxy) return;
        this._proxy.call(
            'GetHistory',
            null,
            Gio.DBusCallFlags.NONE,
            2000,
            null,
            (proxy, result) => {
                try {
                    const res = proxy.call_finish(result);
                    const [historyJson] = res.deepUnpack();
                    const history = JSON.parse(historyJson);
                    if (!Array.isArray(history)) {
                        this._historyRestored = true;
                        return;
                    }
                    // Mark restored before rendering so a render error can't
                    // cause a duplicate restore on the next reconnect.
                    this._historyRestored = true;
                    for (const m of history) {
                        if (!m || (m.role !== 'user' && m.role !== 'assistant'))
                            continue;
                        if (typeof m.content !== 'string' || !m.content.trim())
                            continue;
                        const label = this._addMessage(m.role, m.content);
                        if (m.role === 'assistant') {
                            try {
                                label.clutter_text.set_markup(this._markdownToPango(m.content));
                            } catch (_e) {
                                // Invalid markup — plain text already displayed.
                            }
                        }
                    }
                    if (history.length) {
                        console.log(`[Bubble] Restored ${history.length} messages from history`);
                        this._scrollToBottom();
                    }
                } catch (e) {
                    console.error(`[Bubble] GetHistory error: ${e}`);
                    // The daemon was reachable but the call failed; retry once
                    // shortly, and stop if the extension is disabled.
                    if (!this._historyRestored && this._proxy) {
                        GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
                            if (!this._historyRestored && this._proxy)
                                this._restoreHistory();
                            return GLib.SOURCE_REMOVE;
                        });
                    }
                }
            },
        );
    }

    _clearHistory() {
        this._proxy?.call('ClearHistory', null, Gio.DBusCallFlags.NONE, 2000, null, null);
        this._msgBox.get_children().forEach(c => c.destroy());
    }

    _setStatus(state, detail = '') {
        this._statusState = state || 'hanging-out';
        if (!this._statusLabel) return;
        const d = String(detail ?? '').toLowerCase();

        const map = {
            'hanging-out': 'Hanging out',
            'chatting': 'Chatting',
            'napping': 'Napping',
            'sleeping': 'Sleeping',
            'idle': 'Hanging out',
        };

        let label = map[this._statusState] ?? 'Hanging out';
        if (this._statusState === 'chatting') {
            if (d.includes('consulting second voice'))
                label = 'Second Voice';
            else if (d.includes('integrating second voice'))
                label = 'Synthesizing';
            else if (d.includes('waiting for daemon'))
                label = 'Connecting';
        }
        this._statusLabel.set_text(label);
        this._statusLabel.remove_style_class_name('bubble-status-hanging-out');
        this._statusLabel.remove_style_class_name('bubble-status-chatting');
        this._statusLabel.remove_style_class_name('bubble-status-napping');
        this._statusLabel.remove_style_class_name('bubble-status-sleeping');
        this._statusLabel.add_style_class_name(`bubble-status-${this._statusState}`);
    }

    _setProposalState(hasPending, approved = false) {
        if (!this._proposalLabel || !this._proposalAgreeBtn || !this._proposalRejectBtn)
            return;
        this._proposalLabel.visible = hasPending;
        this._proposalRejectBtn.visible = hasPending;
        this._proposalAgreeBtn.visible = hasPending && !approved;
        this._proposalLabel.set_text(approved ? 'Self draft agreed' : 'Self draft pending');
    }

    _startStreamWatchdog(token) {
        this._clearStreamWatchdog();
        this._streamWatchdogId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            180,
            () => {
                if (!this._streaming || token !== this._currentToken)
                    return GLib.SOURCE_REMOVE;

                console.warn(`[Bubble] Stream watchdog timeout for token ${token}`);
                this._finishStreamWithError('⚠ Response stalled. You can send another message.');
                this._streamWatchdogId = 0;
                return GLib.SOURCE_REMOVE;
            },
        );
    }

    _bumpStreamWatchdog(token) {
        if (!this._streaming || token !== this._currentToken)
            return;
        this._startStreamWatchdog(token);
    }

    _clearStreamWatchdog() {
        if (this._streamWatchdogId) {
            GLib.source_remove(this._streamWatchdogId);
            this._streamWatchdogId = 0;
        }
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------
    _addMessage(role, text) {
        const isUser = role === 'user';

        const row = _boxLayout({
            vertical: false,
            x_expand: true,
            x_align:  isUser
                ? Clutter.ActorAlign.END
                : Clutter.ActorAlign.START,
        });

        if (isUser) {
            const bubble = _boxLayout({
                style_class: 'bubble-msg bubble-msg-user',
                vertical:    true,
            });
            const label = new St.Label({
                text,
                x_expand: true,
            });
            label.clutter_text.line_wrap      = true;
            label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            label.clutter_text.ellipsize      = Pango.EllipsizeMode.NONE;
            label.clutter_text.selectable     = true;
            bubble.add_child(label);
            row.add_child(bubble);
            this._msgBox.add_child(row);
            return label;
        }

        // ── Assistant bubble: vertical column with optional think section ──
        const col = _boxLayout({
            style_class: 'bubble-msg bubble-msg-assistant',
            vertical:    true,
        });

        // Toggle button — hidden until the first reasoning token arrives.
        const thinkToggle = new St.Button({
            style_class: 'bubble-think-toggle',
            label:       '▾ thinking',
            visible:     false,
            x_align:     Clutter.ActorAlign.START,
            reactive:    true,
            can_focus:   false,
        });

        // Reasoning text label — hidden until revealed.
        const thinkBody = new St.Label({
            style_class: 'bubble-think',
            text:        '',
            visible:     false,
            x_expand:    true,
        });
        thinkBody.clutter_text.line_wrap      = true;
        thinkBody.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        thinkBody.clutter_text.ellipsize      = Pango.EllipsizeMode.NONE;
        thinkBody.clutter_text.selectable     = true;

        thinkToggle.connect('clicked', () => {
            thinkBody.visible   = !thinkBody.visible;
            thinkToggle.label   = thinkBody.visible ? '▾ thinking' : '▸ thinking';
        });

        // Answer label — always visible, fills the bubble.
        const answer = new St.Label({
            text:     text,
            x_expand: true,
        });
        answer.clutter_text.line_wrap      = true;
        answer.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        answer.clutter_text.ellipsize      = Pango.EllipsizeMode.NONE;
        answer.clutter_text.selectable     = true;

        col.add_child(thinkToggle);
        col.add_child(thinkBody);
        col.add_child(answer);
        row.add_child(col);
        this._msgBox.add_child(row);

        // Expose to signal handler via instance fields.
        // These are overwritten each time a new assistant message starts.
        this._thinkToggle = thinkToggle;
        this._thinkLabel  = thinkBody;

        return answer;
    }

    _scrollToBottom() {
        // Queue a scroll on the next frame using GLib.idle_add so this
        // method is always synchronous and can never throw into callers.
        GLib.idle_add(GLib.PRIORITY_LOW, () => {
            try {
                const adjustment = this._scrollView?.vscroll?.adjustment;
                if (adjustment)
                    adjustment.value = Math.max(0, adjustment.upper - adjustment.page_size);
            } catch (_e) { /* ignore any version-specific API mismatch */ }
            return GLib.SOURCE_REMOVE;
        });
    }

    // Converts a subset of Markdown to Pango markup.
    // Code spans/blocks are extracted first so bold/italic patterns cannot
    // fire inside them; they are restored after all other transforms.
    _markdownToPango(raw) {
        const esc = t =>
            t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

        // ── Step 1: protect code regions ────────────────────────────────
        const saved = [];
        let   n     = 0;
        let   s     = raw;

        // Fenced code blocks  ```lang\n...\n```
        s = s.replace(/```(?:\w*)\n?([\s\S]*?)```/g, (_, code) => {
            const mark = `\x01B${n++}\x01`;
            saved.push(
                `<span foreground="#8fbcbb"><tt>${esc(code.trim())}</tt></span>`
            );
            return mark;
        });

        // Inline code  `code`
        s = s.replace(/`([^`\n]+)`/g, (_, code) => {
            const mark = `\x01B${n++}\x01`;
            saved.push(`<tt>${esc(code)}</tt>`);
            return mark;
        });

        // ── Step 2: escape remaining text for Pango ──────────────────────
        s = esc(s);

        // ── Step 3: markdown transforms ──────────────────────────────────
        // Bold + italic  ***text***
        s = s.replace(/\*\*\*([^*]+?)\*\*\*/g, '<b><i>$1</i></b>');
        // Bold  **text**
        s = s.replace(/\*\*([^*]+?)\*\*/g, '<b>$1</b>');
        // Italic  *text*  (remaining single-star after bold consumed **)
        s = s.replace(/\*([^*\n]+)\*/g, '<i>$1</i>');
        // Italic  _text_  (only when surrounded by non-underscore)
        s = s.replace(/(?<![\w])_([^_\n]+)_(?![\w])/g, '<i>$1</i>');

        // Headings  (must come before bullet so # is not swallowed)
        s = s.replace(/^### (.+)$/gm, '<b>$1</b>');
        s = s.replace(/^## (.+)$/gm,
            '<span size="large"><b>$1</b></span>');
        s = s.replace(/^# (.+)$/gm,
            '<span size="x-large"><b>$1</b></span>');

        // Bullets  - item  or  * item  (leading whitespace ok)
        s = s.replace(/^[ \t]*[-*] (.+)$/gm, '  \u2022 $1');

        // Numbered lists  — keep number, just indent slightly
        s = s.replace(/^[ \t]*(\d+)\. (.+)$/gm, '  $1. $2');

        // Horizontal rule  ---
        s = s.replace(/^---+$/gm, '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500');

        // ── Step 4: restore protected code regions ───────────────────────
        for (let i = 0; i < saved.length; i++)
            s = s.replace(`\x01B${i}\x01`, saved[i]);

        return s;
    }

    _setInputEnabled(enabled) {
        this._entry.reactive    = enabled;
        this._entry.can_focus   = enabled;
        this._sendBtn.reactive  = enabled;
        if (enabled && this._window?.visible) {
            GLib.idle_add(GLib.PRIORITY_LOW, () => {
                this._entry.grab_key_focus();
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    // -----------------------------------------------------------------------
    // Sidecar
    // -----------------------------------------------------------------------
    _checkSidecar() {
        // Check if sidecar is available
        this._proxy?.call('CheckSidecarAvailable', null, Gio.DBusCallFlags.NONE, 5000, null, null);
    }
}
