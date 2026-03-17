import os
import re
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Pango, GObject, Gdk, Gio

import zellij


_TERMINAL_PALETTES = {
    'argonaut': {
        'fg': '#fffaf4', 'bg': '#0e1019', 'cursor': '#ff0018', 'cursor_fg': '#0e1019',
        'palette': [
            '#232323', '#ff000f', '#8ce10b', '#ffb900',
            '#008df8', '#6d43a6', '#00d8eb', '#ffffff',
            '#444444', '#ff2740', '#abe15b', '#ffd242',
            '#0092ff', '#9a5feb', '#67fff0', '#ffffff',
        ],
    },
    'candyland': {
        'fg': '#fce4f7', 'bg': '#1a0a1e', 'cursor': '#ff6eb4', 'cursor_fg': '#1a0a1e',
        'palette': [
            '#1a0a1e', '#ff5c8a', '#6ee7b7', '#ffcc66',
            '#7cacf8', '#c084fc', '#67e8f9', '#fce4f7',
            '#4a2d5e', '#ff8fab', '#a7f3d0', '#fde68a',
            '#a5b4fc', '#d8b4fe', '#a5f3fc', '#ffffff',
        ],
    },
    'phosphor': {
        'fg': '#33ff00', 'bg': '#060808', 'cursor': '#33ff00', 'cursor_fg': '#060808',
        'palette': [
            '#060808', '#1a7a00', '#33ff00', '#ffb300',
            '#00e5ff', '#1a7a3a', '#00cc88', '#33ff00',
            '#0d1a0d', '#22aa00', '#55ff33', '#ffc933',
            '#33eeff', '#44ff99', '#00ffcc', '#aaffaa',
        ],
    },
    'salt-spray': {
        'fg': '#90d5f0', 'bg': '#012a4a', 'cursor': '#00b4d8', 'cursor_fg': '#012a4a',
        'palette': [
            '#011a2e', '#e05555', '#1a9a6a', '#d4841a',  # 0-3: black, red, green, yellow
            '#0077b6', '#7a5aaa', '#00b4d8', '#90d5f0',  # 4-7: blue, mag, cyan, white
            '#1a4a7a', '#e74c3c', '#3aaed4', '#f4a124',  # 8-11: dim, bred, bgrn, byel
            '#48cae4', '#9a6ac8', '#90e0ef', '#cce8f6',  # 12-15: bblu, bmag, bcyn, bwht
        ],
    },
}


def _ensure_zellij_shell_wrapper():
    """Write (or overwrite) ~/.ProjectMan/zellij-shell-init.sh; return its path.

    This wrapper is set as SHELL when creating new zellij sessions so that
    the initial pane auto-starts `claude -c`. It checks for a per-session
    flag file (~/.ProjectMan/.zellij-init-<session>) and, if present, removes
    it and runs claude, then exits (closing the pane). Subsequent panes in the
    same session find no flag and go straight to the real shell.
    """
    pm_dir = os.path.expanduser('~/.ProjectMan')
    wrapper_path = os.path.join(pm_dir, 'zellij-shell-init.sh')
    script = (
        '#!/bin/bash\n'
        'REAL_SHELL="${ZELLIJ_REAL_SHELL:-/bin/bash}"\n'
        'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"\n'
        'if rm "$INIT_FILE" 2>/dev/null; then\n'
        '    claude -c || claude\n'
        '    exit 0\n'
        'fi\n'
        'exec "$REAL_SHELL" "$@"\n'
    )
    with open(wrapper_path, 'w') as f:
        f.write(script)
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


class TerminalView(Gtk.Box):
    __gsignals__ = {
        'process-exited':   (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        'process-started':  (GObject.SignalFlags.RUN_FIRST, None, ()),
        'process-detached': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, project, settings):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._project = project
        self._settings = settings
        self._child_pid = None
        self._is_multiplexed = False
        self._is_zellij = False
        self._zellij_session = None
        self._font_size = settings.font_size

        self._terminal = Vte.Terminal()
        self._terminal.set_scrollback_lines(settings.scrollback_lines)
        self._terminal.set_audible_bell(settings.audible_bell)
        self._terminal.set_bold_is_bright(True)
        self._terminal.set_hexpand(True)
        self._terminal.set_vexpand(True)
        self._terminal.connect('child-exited', self._on_child_exited)
        self._apply_font()
        self._apply_colors()

        # URL/path matching — opens links on click
        # PCRE2_MULTILINE (0x400) required by VTE's match_add_regex
        url_regex = Vte.Regex.new_for_match(
            r'https?://\S+|file://\S+', -1, 0x400
        )
        self._url_tag = self._terminal.match_add_regex(url_regex, 0)
        self._terminal.match_set_cursor_name(self._url_tag, 'pointer')

        # Plain absolute paths — converted to file:// on click
        path_regex = Vte.Regex.new_for_match(r'/[^\s]+', -1, 0x400)
        self._path_tag = self._terminal.match_add_regex(path_regex, 0)
        self._terminal.match_set_cursor_name(self._path_tag, 'pointer')

        self.append(self._terminal)

        # Intercept Shift+Enter at CAPTURE phase — GTK4/Wayland strips the Shift
        # modifier before VTE sees it; feed kitty keyboard protocol sequence directly.
        self._key_ctrl = Gtk.EventControllerKey.new()
        self._key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._terminal.add_controller(self._key_ctrl)

        # Ctrl+click opens URLs/paths — VTE doesn't claim modified clicks
        self._click_gesture = Gtk.GestureClick.new()
        self._click_gesture.set_button(1)
        self._click_gesture.connect('pressed', self._on_ctrl_click)
        self._terminal.add_controller(self._click_gesture)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        return False

    def _debug(self, msg):
        if self._settings.debug_logging:
            print(f'[DBG] {msg}', flush=True)

    def _on_ctrl_click(self, gesture, n_press, x, y):
        state = gesture.get_current_event_state()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        self._debug(f'click n_press={n_press} x={x:.1f} y={y:.1f} ctrl={ctrl}')
        if not ctrl:
            return
        # GestureClick coordinates on Wayland/GTK4 are root-window-relative,
        # not VTE-widget-relative.  Subtract VTE's position in the root widget.
        try:
            root = self._terminal.get_root()
            coords = self._terminal.translate_coordinates(root, 0, 0)
            if coords:
                vte_x, vte_y = coords[-2], coords[-1]
                x -= vte_x
                y -= vte_y
                self._debug(f'adjusted x={x:.1f} y={y:.1f} (vte_origin={vte_x:.0f},{vte_y:.0f})')
        except Exception as e:
            self._debug(f'translate_coordinates failed: {e}')
        self._open_url_at_coords(x, y)

    def _open_url_at_coords(self, x, y):
        char_w = self._terminal.get_char_width()
        self._debug(f'char_w={char_w}')
        if char_w <= 0:
            return False
        col = int(x / char_w)
        # Use allocated height / row_count for true row pitch (get_char_height
        # excludes line spacing, causing a systematic downward offset)
        row_count = self._terminal.get_row_count()
        widget_h = self._terminal.get_allocated_height()
        char_h = self._terminal.get_char_height()
        if row_count <= 0 or widget_h <= 0:
            return False
        row_height = widget_h / row_count
        row_from_top = int(y / row_height)
        self._debug(f'widget_h={widget_h} row_count={row_count} char_h={char_h} row_height={row_height:.2f}')
        self._debug(f'col={col} row_from_top={row_from_top}')
        try:
            visible_rows = self._terminal.get_row_count()
            result = self._terminal.get_text_range_format(
                Vte.Format.TEXT, 0, 0, visible_rows - 1, self._terminal.get_column_count()
            )
            text = result[0] if isinstance(result, tuple) else result
            if not text:
                self._debug('empty text')
                return False
            lines = text.split('\n')
            if row_from_top >= len(lines):
                self._debug(f'row_from_top={row_from_top} >= lines={len(lines)}')
                return False
            line = lines[row_from_top]
            self._debug(f'line={repr(line[:80])}')
            # Dump all non-empty lines so we can see the full buffer layout
            for i, l in enumerate(lines):
                stripped = l.strip()
                if stripped:
                    self._debug(f'  buf[{i:02d}]={repr(stripped[:70])}')
        except Exception as e:
            self._debug(f'text extraction error: {e}')
            return False
        url_pat = re.compile(r'https?://\S+|file://\S+|/[^\s]+')
        matches = list(url_pat.finditer(line))
        self._debug(f'matches={[(m.group(), m.start(), m.end()) for m in matches]}')
        for m in matches:
            if m.start() <= col <= m.end():
                url = re.sub(r'[)\].,;!?\'"]+$', '', m.group())
                uri = ('file://' + url) if url.startswith('/') else url
                self._debug(f'launching {uri}')
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
        self._debug(f'no match at col={col}')
        return False

    def _apply_font(self):
        desc = Pango.FontDescription.from_string(f'Monospace {self._font_size}')
        self._terminal.set_font(desc)

    def _apply_colors(self):
        def rgba(hex_str):
            c = Gdk.RGBA()
            c.parse(hex_str)
            return c

        theme = getattr(self._settings, 'theme', 'argonaut')
        p = _TERMINAL_PALETTES.get(theme, _TERMINAL_PALETTES['argonaut'])
        fg = rgba(p['fg'])
        bg = rgba(p['bg'])
        palette = [rgba(h) for h in p['palette']]
        self._terminal.set_colors(fg, bg, palette)
        self._terminal.set_color_cursor(rgba(p['cursor']))
        self._terminal.set_color_cursor_foreground(rgba(p['cursor_fg']))

    def spawn_claude(self, session_id=None, fresh=False, project_name=None):
        self._kill_child()
        self._terminal.reset(True, True)
        claude_cmd = self._settings.resolved_claude_binary
        if session_id:
            argv = [claude_cmd, '--resume', session_id]
        elif fresh:
            argv = [claude_cmd]
        else:
            # Try continuing most recent conversation; fall back to fresh
            # if there's no history (claude -c exits non-zero).
            import shlex
            c = shlex.quote(claude_cmd)
            argv = ['bash', '-c', f'{c} -c || exec {c}']
        self._is_multiplexed = False
        self._spawn(argv)

    def spawn_zellij(self, session_name):
        """Attach to or create a zellij session for this project.

        New sessions: created with a shell wrapper that auto-launches `claude -c`
        in the initial pane, then drops to the real shell.
        Existing sessions: attached with `zellij attach <name>`.
        """
        self._kill_child()
        self._terminal.reset(True, True)
        self._is_zellij = True
        self._zellij_session = session_name
        self._is_multiplexed = True
        alive = zellij.session_alive(session_name)
        if alive:
            cmd = ['zellij', 'attach', session_name]
            env = None
        else:
            socket_path = os.path.join(zellij.socket_dir(), session_name)
            try:
                os.unlink(socket_path)
            except OSError:
                pass
            # Create per-session init flag; wrapper reads it to auto-start claude
            flag_path = os.path.join(
                os.path.expanduser('~/.ProjectMan'), f'.zellij-init-{session_name}'
            )
            open(flag_path, 'w').close()
            wrapper = _ensure_zellij_shell_wrapper()
            env = dict(os.environ)
            env['SHELL'] = wrapper
            env['ZELLIJ_REAL_SHELL'] = os.environ.get('SHELL', '/bin/bash')
            cmd = ['zellij', 'attach', '--create', session_name]
        self._spawn(cmd, env)

    def deactivate(self):
        """Gracefully stop the child; terminal output is preserved for context."""
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # child-exited signal will fire and emit process-exited

    def _spawn(self, argv, env=None):
        envv = [f'{k}={v}' for k, v in env.items()] if env is not None else None
        self._terminal.spawn_async(
            Vte.PtyFlags(0),
            self._project.path,
            argv,
            envv,
            GLib.SpawnFlags.SEARCH_PATH,
            None,                           # child_setup
            None,                           # child_setup_data (hidden from __doc__)
            -1,                             # timeout
            None,                           # cancellable
            self._on_spawn_done,            # callback
        )

    def _on_spawn_done(self, terminal, pid, error):
        if pid == -1:
            self._child_pid = None
        else:
            self._child_pid = pid
            self.emit('process-started')

    def _on_child_exited(self, terminal, status):
        self._child_pid = None
        if self._is_zellij and self._zellij_session:
            if zellij.session_alive(self._zellij_session):
                self.emit('process-detached')
                return
            # Session is truly gone — clear zellij state before emitting exited
            self._is_zellij = False
            self._zellij_session = None
        self.emit('process-exited', status)

    def _kill_child(self):
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            self._child_pid = None
            self._terminal.reset(True, True)

    def zoom_in(self):
        self._font_size = min(self._font_size + 1, 36)
        self._apply_font()

    def zoom_out(self):
        self._font_size = max(self._font_size - 1, 6)
        self._apply_font()

    def zoom_reset(self):
        self._font_size = self._settings.font_size
        self._apply_font()

    def apply_settings(self, settings):
        # Note: resets font_size to the settings default, discarding any active zoom offset.
        self._settings = settings
        self._font_size = settings.font_size
        self._apply_font()
        self._apply_colors()
        self._terminal.set_scrollback_lines(settings.scrollback_lines)
        self._terminal.set_audible_bell(settings.audible_bell)

    def get_terminal(self):
        return self._terminal
