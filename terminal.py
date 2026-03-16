import os
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Pango, GObject, Gdk


class TerminalView(Gtk.Box):
    __gsignals__ = {
        'process-exited': (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        'process-started': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, project):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._project = project
        self._child_pid = None
        self._font_size = 11

        self._terminal = Vte.Terminal()
        self._terminal.set_scrollback_lines(10000)
        self._terminal.set_audible_bell(False)
        self._terminal.set_bold_is_bright(True)
        self._terminal.set_hexpand(True)
        self._terminal.set_vexpand(True)
        self._terminal.connect('child-exited', self._on_child_exited)
        self._apply_font()
        self.append(self._terminal)

        # Intercept Shift+Enter at CAPTURE phase — GTK4/Wayland strips the Shift
        # modifier before VTE sees it; feed kitty keyboard protocol sequence directly.
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._terminal.add_controller(key_ctrl)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        return False

    def _apply_font(self):
        desc = Pango.FontDescription.from_string(f'Monospace {self._font_size}')
        self._terminal.set_font(desc)

    def spawn_claude(self, session_id=None, fresh=False):
        self._kill_child()
        self._terminal.reset(True, True)
        argv = ['claude']
        if session_id:
            argv += ['--resume', session_id]
        elif not fresh:
            argv += ['-c']
        self._spawn(argv)

    def spawn_bash(self):
        self._kill_child()
        self._terminal.reset(True, True)
        self._spawn(['/bin/bash'])

    def deactivate(self):
        """Gracefully stop the child; terminal output is preserved for context."""
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # child-exited signal will fire and emit process-exited

    def _spawn(self, argv):
        self._terminal.spawn_async(
            Vte.PtyFlags(0),
            self._project.path,
            argv,
            None,                           # envv
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
        self._font_size = 11
        self._apply_font()

    def get_terminal(self):
        return self._terminal
