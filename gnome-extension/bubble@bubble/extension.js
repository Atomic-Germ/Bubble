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

// ---------------------------------------------------------------------------
// D-Bus interface
// ---------------------------------------------------------------------------
const DBUS_IFACE = `<node>
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
</node>`;

// Interface info is not needed for proxy.call() + g-signal; pass null below.

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------
export default class BubbleExtension extends Extension {

    enable() {
        this._proxy        = null;
        this._proxySigs    = [];
        this._window       = null;
        this._streaming    = false;
        this._currentToken = null;
        this._streamLabel  = null;

        this._buildUI();
        this._connectDaemon();
        this._registerKeybinding();
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
    }

    // -----------------------------------------------------------------------
    // D-Bus
    // -----------------------------------------------------------------------
    _connectDaemon() {
        Gio.DBusProxy.new(
            Gio.DBus.session,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
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
                        const token    = unpacked[0];

                        if (signalName === 'StreamChunk') {
                            const text = unpacked[1];
                            if (token !== this._currentToken) return;
                            const cur = this._streamLabel?.get_text() ?? '';
                            this._streamLabel?.set_text(cur + text);
                            this._scrollToBottom();

                        } else if (signalName === 'StreamDone') {
                            if (token !== this._currentToken) return;
                            this._streaming    = false;
                            this._currentToken = null;
                            this._streamLabel  = null;
                            this._setInputEnabled(true);

                        } else if (signalName === 'StreamError') {
                            const errMsg = unpacked[1];
                            if (token !== this._currentToken) return;
                            this._streaming    = false;
                            this._currentToken = null;
                            if (this._streamLabel) {
                                this._streamLabel.add_style_class_name('bubble-error');
                                this._streamLabel.set_text(`⚠ ${errMsg}`);
                                this._streamLabel = null;
                            }
                            this._setInputEnabled(true);
                        }
                    }),
                );

                console.log('[Bubble] D-Bus proxy connected.');
            },
        );
    }

    // -----------------------------------------------------------------------
    // UI construction
    // -----------------------------------------------------------------------
    _buildUI() {
        // ── Root container ────────────────────────────────────────────────
        this._window = new St.BoxLayout({
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
        const header = new St.BoxLayout({
            style_class: 'bubble-header',
            vertical:    false,
        });

        const title = new St.Label({
            style_class: 'bubble-title',
            text:        '⬡  Bubble',
            y_align:     Clutter.ActorAlign.CENTER,
            x_expand:    true,
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

        this._msgBox = new St.BoxLayout({
            style_class: 'bubble-msgs',
            vertical:    true,
            x_expand:    true,
        });
        this._scrollView.set_child(this._msgBox);

        // ── Input bar ─────────────────────────────────────────────────────
        const inputRow = new St.BoxLayout({
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

        const text = this._entry.get_text().trim();
        if (!text) return;

        this._entry.set_text('');
        this._addMessage('user', text);
        this._setInputEnabled(false);
        this._streaming = true;

        // Generate the UUID here so _currentToken is set synchronously,
        // before any D-Bus round-trip. This eliminates the race where a
        // very fast NPU emits StreamDone before call_finish() runs.
        const token = GLib.uuid_string_random();
        this._currentToken = token;

        // Pre-create the assistant bubble so chunks stream into it
        this._streamLabel = this._addMessage('assistant', '');

        this._proxy.call(
            'SendMessage',
            new GLib.Variant('(ss)', [text, token]),
            Gio.DBusCallFlags.NONE,
            5000,
            null,
            (_proxy, result) => {
                try {
                    _proxy.call_finish(result);
                } catch (e) {
                    console.error(`[Bubble] SendMessage error: ${e}`);
                    this._streamLabel?.add_style_class_name('bubble-error');
                    this._streamLabel?.set_text(`⚠ ${e.message}`);
                    this._streamLabel  = null;
                    this._streaming    = false;
                    this._currentToken = null;
                    this._setInputEnabled(true);
                }
            },
        );
    }

    _clearHistory() {
        this._proxy?.call('ClearHistory', null, Gio.DBusCallFlags.NONE, 2000, null, null);
        this._msgBox.get_children().forEach(c => c.destroy());
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------
    _addMessage(role, text) {
        const isUser = role === 'user';

        const row = new St.BoxLayout({
            vertical: false,
            x_expand: true,
            x_align:  isUser
                ? Clutter.ActorAlign.END
                : Clutter.ActorAlign.START,
        });

        const label = new St.Label({
            style_class: isUser
                ? 'bubble-msg bubble-msg-user'
                : 'bubble-msg bubble-msg-assistant',
            text,
        });
        label.clutter_text.line_wrap      = true;
        label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        label.clutter_text.ellipsize      = Pango.EllipsizeMode.NONE;

        row.add_child(label);
        this._msgBox.add_child(row);
        this._scrollToBottom();
        return label;
    }

    _scrollToBottom() {
        GLib.idle_add(GLib.PRIORITY_LOW, () => {
            try {
                // vadjustment is a direct property on St.ScrollView (GNOME 47+).
                // Fall back to the older get_vscroll_bar() API defensively.
                const adj = this._scrollView?.vadjustment
                         ?? this._scrollView?.get_vscroll_bar?.()?.get_adjustment?.();
                if (adj)
                    adj.value = adj.upper - adj.page_size;
            } catch (_e) {
                // Swallow silently — never let a scroll error loop the idle queue.
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _setInputEnabled(enabled) {
        this._entry.reactive    = enabled;
        this._entry.can_focus   = enabled;
        this._sendBtn.reactive  = enabled;
    }
}
