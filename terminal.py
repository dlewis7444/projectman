import os
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Pango, GObject, Gdk, Gio

import zellij


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

        # URL matching — opens links on click
        url_regex = Vte.Regex.new_for_match(
            r'https?://\S+|file://\S+', -1, 0
        )
        self._url_tag = self._terminal.match_add_regex(url_regex, 0)
        self._terminal.match_set_cursor_name(self._url_tag, 'pointer')

        self.append(self._terminal)

        # Intercept Shift+Enter at CAPTURE phase — GTK4/Wayland strips the Shift
        # modifier before VTE sees it; feed kitty keyboard protocol sequence directly.
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._terminal.add_controller(key_ctrl)

        click_ctrl = Gtk.GestureClick.new()
        click_ctrl.connect('released', self._on_terminal_click)
        self._terminal.add_controller(click_ctrl)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        return False

    def _on_terminal_click(self, gesture, n_press, x, y):
        url, _tag = self._terminal.match_check_event(gesture.get_last_event(None))
        if url:
            Gio.AppInfo.launch_default_for_uri(url, None)

    def _apply_font(self):
        desc = Pango.FontDescription.from_string(f'Monospace {self._font_size}')
        self._terminal.set_font(desc)

    def _apply_colors(self):
        def rgba(hex_str):
            c = Gdk.RGBA()
            c.parse(hex_str)
            return c

        fg = rgba('#fffaf4')
        bg = rgba('#0e1019')
        palette = [
            rgba('#232323'),  #  0 black
            rgba('#ff000f'),  #  1 red
            rgba('#8ce10b'),  #  2 green
            rgba('#ffb900'),  #  3 yellow
            rgba('#008df8'),  #  4 blue
            rgba('#6d43a6'),  #  5 magenta
            rgba('#00d8eb'),  #  6 cyan
            rgba('#ffffff'),  #  7 white
            rgba('#444444'),  #  8 bright black
            rgba('#ff2740'),  #  9 bright red
            rgba('#abe15b'),  # 10 bright green
            rgba('#ffd242'),  # 11 bright yellow
            rgba('#0092ff'),  # 12 bright blue
            rgba('#9a5feb'),  # 13 bright magenta
            rgba('#67fff0'),  # 14 bright cyan
            rgba('#ffffff'),  # 15 bright white
        ]
        self._terminal.set_colors(fg, bg, palette)
        self._terminal.set_color_cursor(rgba('#ff0018'))
        self._terminal.set_color_cursor_foreground(rgba('#0e1019'))

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
        self._terminal.set_scrollback_lines(settings.scrollback_lines)
        self._terminal.set_audible_bell(settings.audible_bell)

    def get_terminal(self):
        return self._terminal
