import os
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Adw, Vte, GLib, Gdk, Pango

from terminal import _TERMINAL_PALETTES


class PAAWindow(Adw.Window):
    """Projects Admin Agent overlay window."""

    def __init__(self, parent, settings, store):
        super().__init__()
        self._settings = settings
        self._child_pid = None

        # Window properties
        self.set_title('Projects Admin Agent')
        self.set_transient_for(parent)
        self.set_modal(False)

        # Size to ~90% of parent
        pw = parent.get_width()
        ph = parent.get_height()
        self.set_default_size(int(pw * 0.9), int(ph * 0.9))

        # Escape to close
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

        # Clean up child process on close
        self.connect('close-request', self._on_close_request)

        # Build layout
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(4)

        # -- Widget bar --
        widget_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        projects_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        projects_widget.add_css_class('paa-widget')
        projects_widget.set_tooltip_text('Active and archived project counts')

        title_lbl = Gtk.Label(label='PROJECTS')
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.add_css_class('paa-widget-title')
        projects_widget.append(title_lbl)

        active_count = len(store.load_projects())
        active_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        active_num = Gtk.Label(label=str(active_count))
        active_num.add_css_class('paa-widget-count')
        active_row.append(active_num)
        active_lbl = Gtk.Label(label='active')
        active_lbl.add_css_class('dim-label')
        active_lbl.set_valign(Gtk.Align.END)
        active_lbl.set_margin_bottom(3)
        active_row.append(active_lbl)
        projects_widget.append(active_row)

        archived_count = len(store.load_archived())
        archived_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        archived_num = Gtk.Label(label=str(archived_count))
        archived_num.add_css_class('paa-widget-sub')
        archived_row.append(archived_num)
        archived_lbl = Gtk.Label(label='archived')
        archived_lbl.add_css_class('dim-label')
        archived_lbl.add_css_class('caption')
        archived_row.append(archived_lbl)
        projects_widget.append(archived_row)

        widget_bar.append(projects_widget)
        content.append(widget_bar)

        # -- Button bar --
        button_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        placeholder_btn = Gtk.Button()
        placeholder_btn.set_child(Gtk.Label(label='\U0001F4CB'))
        placeholder_btn.add_css_class('flat')
        placeholder_btn.add_css_class('circular')
        placeholder_btn.set_tooltip_text('Placeholder')
        button_bar.append(placeholder_btn)
        content.append(button_bar)

        # -- VTE Terminal --
        self._terminal = Vte.Terminal()
        self._terminal.set_hexpand(True)
        self._terminal.set_vexpand(True)
        self._terminal.set_scrollback_lines(settings.scrollback_lines)

        # Font
        desc = Pango.FontDescription.from_string(f'Monospace {settings.font_size}')
        self._terminal.set_font(desc)

        # Colors from theme
        theme = getattr(settings, 'theme', 'argonaut')
        p = _TERMINAL_PALETTES.get(theme, _TERMINAL_PALETTES['argonaut'])
        def rgba(hex_str):
            c = Gdk.RGBA()
            c.parse(hex_str)
            return c
        self._terminal.set_colors(
            rgba(p['fg']), rgba(p['bg']),
            [rgba(h) for h in p['palette']],
        )
        self._terminal.set_color_cursor(rgba(p['cursor']))
        self._terminal.set_color_cursor_foreground(rgba(p['cursor_fg']))

        content.append(self._terminal)

        # -- Status bar --
        paa_dir = os.path.join(
            settings.resolved_projects_dir, '.project-admin-agent'
        )
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        status_bar.add_css_class('paa-status-bar')
        status_bar.set_tooltip_text('Session info')

        session_lbl = Gtk.Label(label='Session: fresh')
        session_lbl.set_halign(Gtk.Align.START)
        session_lbl.set_hexpand(True)
        status_bar.append(session_lbl)

        path_lbl = Gtk.Label(label=paa_dir)
        path_lbl.set_halign(Gtk.Align.END)
        status_bar.append(path_lbl)

        content.append(status_bar)

        toolbar_view.set_content(content)
        self.set_content(toolbar_view)

    def spawn_claude(self, paa_dir):
        """Spawn claude 'go' in the PAA directory."""
        claude_cmd = self._settings.resolved_claude_binary
        self._terminal.spawn_async(
            Vte.PtyFlags(0),
            paa_dir,
            [claude_cmd, 'go'],
            None,
            GLib.SpawnFlags.SEARCH_PATH,
            None, None, -1, None,
            self._on_spawn_done,
        )

    def _on_spawn_done(self, terminal, pid, error):
        if pid == -1:
            self._child_pid = None
        else:
            self._child_pid = pid

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_close_request(self, window):
        self._kill_child()
        return False  # allow close to proceed

    def _kill_child(self):
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                os.waitpid(self._child_pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self._child_pid = None
