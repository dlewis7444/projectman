import os
import re
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Pango, GObject, Gdk, Gio

import terminal_copy
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
        # PM-owned pidfd watches, one per spawned child. Each entry is a dict
        # {pid, fd, source_id}. Vte's reaper is no longer in the picture —
        # _spawn does its own fork+exec and never calls watch_child. See
        # docs/pidfd-leak-investigation.md.
        self._watches = []

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

        scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=self._terminal.get_vadjustment(),
        )
        term_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        term_box.set_hexpand(True)
        term_box.set_vexpand(True)
        term_box.append(self._terminal)
        term_box.append(scrollbar)
        self.append(term_box)

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

        # Right-click context menu
        self._rclick_gesture = Gtk.GestureClick.new()
        self._rclick_gesture.set_button(3)
        self._rclick_gesture.connect('pressed', self._on_right_click)
        self._terminal.add_controller(self._rclick_gesture)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._smart_copy()
                return True
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._terminal.paste_clipboard()
                return True
        return False

    def _debug(self, msg):
        if self._settings.debug_logging:
            print(f'[DBG] {msg}', flush=True)

    def _on_ctrl_click(self, gesture, n_press, x, y):
        # GestureClick.get_current_event_state() and get_last_event() both
        # drop modifiers on Wayland/GTK4 for mouse clicks. Query the live
        # keyboard modifier state from the seat instead.
        state = 0
        display = Gdk.Display.get_default()
        if display is not None:
            seat = display.get_default_seat()
            if seat is not None:
                keyboard = seat.get_keyboard()
                if keyboard is not None:
                    state = keyboard.get_modifier_state()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        self._debug(f'click n_press={n_press} x={x:.1f} y={y:.1f} ctrl={ctrl} state={int(state)}')
        if not ctrl:
            return
        self._open_url_at_coords(x, y)

    def _url_at(self, x, y):
        """Return URL/path string at pixel coords (x, y), or None.

        Uses VTE's check_match_at() so the regexes registered earlier with
        match_add_regex() do the work — including all the buffer/viewport
        coordinate translation and scrollback awareness that the previous
        manual implementation got wrong (it indexed scrollback rows with
        viewport row numbers, so any active scrollback turned every click
        into a "no match").
        """
        try:
            match, _tag = self._terminal.check_match_at(x, y)
        except Exception:
            return None
        if not match:
            return None
        # The registered \S+ regex greedily eats trailing punctuation.
        return re.sub(r'[)\].,;!?\'"]+$', '', match)

    def _open_url_at_coords(self, x, y):
        url = self._url_at(x, y)
        if not url:
            self._debug(f'no match at ({x:.0f}, {y:.0f})')
            return False
        uri = ('file://' + url) if url.startswith('/') else url
        self._debug(f'launching {uri}')
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return True

    def _on_right_click(self, gesture, n_press, x, y):
        url = self._url_at(x, y)
        self._show_context_menu(int(x), int(y), url)

    def _show_context_menu(self, x, y, url):
        popover = Gtk.Popover()
        popover.set_parent(self._terminal)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, y, 1, 1
        popover.set_pointing_to(rect)
        popover.connect('closed', lambda p: p.unparent())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.add_css_class('term-context-menu')
        box.set_size_request(160, -1)

        def item(label, callback, sensitive=True):
            btn = Gtk.Button()
            lbl = Gtk.Label(label=label)
            lbl.set_halign(Gtk.Align.START)
            btn.set_child(lbl)
            btn.add_css_class('flat')
            btn.set_sensitive(sensitive)
            btn.set_halign(Gtk.Align.FILL)
            btn.connect('clicked', lambda _b, cb=callback: (cb(), popover.popdown()))
            return btn

        has_sel = self._terminal.get_has_selection()
        box.append(item('Copy', self._smart_copy, has_sel))
        box.append(item('Paste', self._terminal.paste_clipboard))
        box.append(item('Select All', self._terminal.select_all))

        if url:
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            uri = ('file://' + url) if url.startswith('/') else url
            box.append(item('Open URL', lambda u=uri: Gio.AppInfo.launch_default_for_uri(u, None)))
            box.append(item('Copy URL', lambda u=url: self._set_clipboard(u)))

        popover.set_child(box)
        popover.popup()

    def _set_clipboard(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)

    def _smart_copy(self):
        """Copy selection with TUI-emitted hard-wrap artifacts collapsed.

        The selection from VTE already has `\\n  ` / `\\n   ` patterns where
        the inner program (Claude Code etc.) hard-wrapped long paragraphs at
        a 2-space hanging indent. `collapse_hard_wraps` rejoins those visual
        wraps back into flowing prose, leaving paragraph breaks, code
        indents, and short structural lines alone.

        Falls back to VTE's native copy on any error — worst case is today's
        pre-fix behavior.
        """
        try:
            if not self._terminal.get_has_selection():
                return
            baseline = self._terminal.get_text_selected(Vte.Format.TEXT)
            if not baseline:
                self._terminal.copy_clipboard_format(Vte.Format.TEXT)
                return
            result = terminal_copy.collapse_hard_wraps(baseline)
            self._debug(
                f'smart-copy baseline_len={len(baseline)} result_len={len(result)} '
                f'head={result[:80]!r}'
            )
            Gdk.Display.get_default().get_clipboard().set(result)
        except Exception as e:
            self._debug(f'smart-copy failed: {e!r}; falling back to native copy')
            try:
                self._terminal.copy_clipboard_format(Vte.Format.TEXT)
            except Exception:
                pass

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
            # Try continuing most recent conversation; fall back to fresh if
            # there's no history (claude -c exits non-zero).  Two guards keep
            # PM's SIGTERM/SIGHUP from accidentally respawning claude:
            #   - exit-code test rejects signal kills (s > 128)
            #   - TERM/HUP trap rejects graceful-but-nonzero exits, where
            #     claude catches the signal and cleanly returns code 1-128.
            #     Without the trap, that case slips past the exit-code test
            #     and bash exec's a fresh claude that never saw the signal,
            #     leaving the project stuck "active" until SIGKILL.
            import shlex
            c = shlex.quote(claude_cmd)
            argv = ['bash', '-c',
                    f"trap 'exit 143' TERM HUP; {c} -c; s=$?; "
                    f'[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec {c}']
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
            for pid in (-self._child_pid, self._child_pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            # child-exited signal will fire and emit process-exited

    def _spawn(self, argv, env=None):
        """DIY fork + exec — PM owns the child watch end-to-end.

        Replaces Vte.Terminal.spawn_async, which routes through vte's
        G_PRIORITY_LOW glib reaper. That reaper has been observed to leak
        pidfds (Pid: -1 fds accumulate, the main-loop poll() returns
        instantly forever, CPU pegs) and to miss child-exited dispatch
        (tab stuck "attached"). See docs/pidfd-leak-investigation.md.

        We create the pty via Vte.Pty.new_sync (cheap, no thread), fork in
        Python, let vte's own Pty.child_setup do the controlling-tty dance
        on the child side, then execvp. On the parent side we set the pty
        on the terminal (no watch_child call), open our own pidfd, and
        register a unix-fd watch at G_PRIORITY_DEFAULT. We never call
        vte_terminal_watch_child, so vte's reaper never gets a pidfd and
        can't leak one.
        """
        try:
            pty = Vte.Pty.new_sync(Vte.PtyFlags(0), None)
        except GLib.Error:
            self._child_pid = None
            return
        pty.set_size(
            self._terminal.get_row_count(),
            self._terminal.get_column_count(),
        )

        working_dir = self._project.path
        argv_list = list(argv)
        # Vte.Terminal.spawn_async used to inject TERM/COLORTERM into the child
        # env for us; the DIY fork+exec path (pty.child_setup only touches the
        # controlling tty) does not. Launched from a desktop launcher, PM has
        # no TERM of its own, so claude would render without color. setdefault
        # leaves a real inherited TERM alone.
        env_dict = dict(env) if env is not None else dict(os.environ)
        env_dict.setdefault('TERM', 'xterm-256color')
        env_dict.setdefault('COLORTERM', 'truecolor')

        pid = os.fork()
        if pid == 0:
            # CHILD. Use only async-signal-safe / pre-exec-safe operations.
            try:
                try:
                    os.chdir(working_dir)
                except OSError:
                    pass
                # vte handles setsid, TIOCSCTTY, dup2(peer, 0/1/2), reset signals
                pty.child_setup()
                os.execvpe(argv_list[0], argv_list, env_dict)
            except Exception:
                pass
            os._exit(127)

        # PARENT.
        self._terminal.set_pty(pty)
        self._child_pid = pid
        self._add_pidfd_watch(pid)
        self.emit('process-started')

    def _on_child_exited(self, terminal, status):
        """Vte's signal — never fires in our DIY-spawn path (we don't call
        watch_child). Left connected defensively in case some other code
        path ever does, so we don't lose the signal."""
        self._fire_exit_if_current(self._child_pid, status)

    def _add_pidfd_watch(self, pid):
        try:
            fd = os.pidfd_open(pid, 0)
        except (OSError, ProcessLookupError):
            # Child exited between spawn_finish and pidfd_open. Reap and
            # synthesize the exit on the next main-loop iteration.
            self._reap(pid)
            GLib.idle_add(self._fire_exit_if_current, pid, 0)
            return
        source_id = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            fd,
            GLib.IOCondition.IN,
            self._on_pidfd_ready,
            pid,
        )
        self._watches.append({'pid': pid, 'fd': fd, 'source_id': source_id})

    def _on_pidfd_ready(self, fd, condition, pid):
        status = self._reap(pid)
        try:
            os.close(fd)
        except OSError:
            pass
        self._watches = [w for w in self._watches if w['fd'] != fd]
        self._fire_exit_if_current(pid, status)
        return False  # one-shot — remove the source

    def _reap(self, pid):
        try:
            r = os.waitpid(pid, os.WNOHANG)
            if r and r[0] == pid:
                return r[1]
        except (ChildProcessError, OSError):
            pass
        return 0

    def _fire_exit_if_current(self, pid, status):
        """Emit process-exited only if this pid is the one we're tracking.

        Stale exits (e.g. an old child dying after the user already restarted
        the session) are silently dropped — their pidfd has been cleaned up,
        and the UI has already moved on.
        """
        if pid != self._child_pid:
            return False
        self._child_pid = None
        if self._is_zellij and self._zellij_session:
            if zellij.session_alive(self._zellij_session):
                self.emit('process-detached')
                return False
            self._is_zellij = False
            self._zellij_session = None
        self.emit('process-exited', status)
        return False  # for idle_add — single-shot

    def check_child_alive(self):
        """Defensive backup for our pidfd watch.

        Our pidfd watch (registered in _add_pidfd_watch) is the primary
        mechanism for detecting child exit. This poll is kept as a belt-and-
        suspenders catch in case the watch ever misses (it shouldn't — we
        register at G_PRIORITY_DEFAULT, not vte's G_PRIORITY_LOW, and we own
        the pidfd lifecycle ourselves). If we detect a gone/zombied child,
        reap and synthesize the exit so the UI catches up.
        """
        pid = self._child_pid
        if pid is None:
            return
        try:
            with open(f'/proc/{pid}/status') as f:
                state = next(
                    (line.split()[1] for line in f if line.startswith('State:')),
                    None,
                )
        except FileNotFoundError:
            state = 'gone'
        except OSError:
            return
        if state not in ('Z', 'gone'):
            return
        status = self._reap(pid)
        # Guard against a racing respawn: only fire if the PID hasn't changed.
        self._fire_exit_if_current(pid, status)

    def _kill_child(self):
        if self._child_pid is not None:
            for pid in (-self._child_pid, self._child_pid):
                try:
                    os.kill(pid, signal.SIGHUP)
                except (ProcessLookupError, OSError):
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
