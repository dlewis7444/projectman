import os
import signal
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Adw, Vte, GLib, Gdk, Pango

from terminal import _TERMINAL_PALETTES


class PAAWindow(Adw.Window):
    """Projects Admin Agent overlay window."""

    def __init__(self, parent, settings, store, watcher=None, on_close=None):
        super().__init__()
        self._settings = settings
        self._watcher = watcher
        self._child_pid = None
        self._spawn_cancelled = False
        self._closing = False
        self._on_close_cb = on_close

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

        # Intercept close-request: kill child and schedule explicit destroy.
        # Returning True prevents Adw.Window's default close (which may hide
        # rather than destroy in some libadwaita versions), ensuring the destroy
        # signal always fires and the singleton reference is always cleared.
        self.connect('close-request', self._on_close_request)
        # Fallback: if destroy fires without close-request (e.g. parent destroyed)
        self.connect('destroy', self._on_destroy)

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

        # -- STATUS widget --
        if watcher:
            statuses = [watcher.get_project_status(p) for p in store.load_projects()]
            working = statuses.count('working')
            waiting = statuses.count('waiting')
            busy = working + waiting
            main_text = str(busy)
            sub_text = f'{working} working / {waiting} waiting'
        else:
            main_text, sub_text = '—', 'unavailable'

        widget_bar.append(self._make_widget(
            'STATUS', main_text, sub_text, 'Active claude sessions by state'
        ))

        # -- DISK widget --
        try:
            st = os.statvfs(settings.resolved_projects_dir)
            free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
            disk_main = f'{free_gb:.1f} GB'
            disk_sub = 'free on disk'
        except OSError:
            disk_main, disk_sub = '—', 'unavailable'

        widget_bar.append(self._make_widget('DISK', disk_main, disk_sub, 'Free space on projects filesystem'))

        # -- SNAPSHOT AGE widget --
        snapshot_path = os.path.join(settings.resolved_projects_dir, '.project-admin-agent', '.system', 'project-snapshot.md')
        try:
            age_s = int(time.time() - os.path.getmtime(snapshot_path))
            if age_s < 60:
                age_text = '< 1m'
            elif age_s < 3600:
                age_text = f'{age_s // 60}m'
            else:
                age_text = f'{age_s // 3600}h'
            snap_sub = 'snapshot age'
        except OSError:
            age_text, snap_sub = '—', 'not found'

        widget_bar.append(self._make_widget('SNAPSHOT', age_text, snap_sub, 'Age of project-snapshot.md'))

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

        self._terminal.connect('child-exited', self._on_child_exited)

        # Intercept Shift+Enter at CAPTURE phase on the VTE widget — GTK4/Wayland
        # strips the Shift modifier before VTE sees it; feed kitty protocol directly.
        term_key_ctrl = Gtk.EventControllerKey.new()
        term_key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        term_key_ctrl.connect('key-pressed', self._on_terminal_key_pressed)
        self._terminal.add_controller(term_key_ctrl)

        # Right-click context menu
        self._rclick_gesture = Gtk.GestureClick.new()
        self._rclick_gesture.set_button(3)
        self._rclick_gesture.connect('pressed', self._on_right_click)
        self._terminal.add_controller(self._rclick_gesture)

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

    @staticmethod
    def _make_widget(title, main_text, sub_text, tooltip=''):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class('paa-widget')
        if tooltip:
            box.set_tooltip_text(tooltip)
        title_lbl = Gtk.Label(label=title)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.add_css_class('paa-widget-title')
        box.append(title_lbl)
        main_lbl = Gtk.Label(label=main_text)
        main_lbl.add_css_class('paa-widget-count')
        main_lbl.set_halign(Gtk.Align.START)
        box.append(main_lbl)
        sub_lbl = Gtk.Label(label=sub_text)
        sub_lbl.add_css_class('paa-widget-sub')
        sub_lbl.set_halign(Gtk.Align.START)
        box.append(sub_lbl)
        return box

    def spawn_claude(self, paa_dir):
        """Spawn claude 'WELCOME' in the PAA directory."""
        self._spawn_cancelled = False
        claude_cmd = self._settings.resolved_claude_binary
        self._terminal.spawn_async(
            Vte.PtyFlags(0),
            paa_dir,
            [claude_cmd, 'WELCOME'],
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
            if self._spawn_cancelled:
                self._kill_child()

    def _on_child_exited(self, terminal, status):
        """VTE child process exited naturally — just clear the PID."""
        self._child_pid = None

    def _on_terminal_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._terminal.copy_clipboard_format(Vte.Format.TEXT)
                return True
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._terminal.paste_clipboard()
                return True
        return False

    def _on_right_click(self, gesture, n_press, x, y):
        self._show_context_menu(int(x), int(y))

    def _show_context_menu(self, x, y):
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
        box.append(item('Copy', lambda: self._terminal.copy_clipboard_format(Vte.Format.TEXT), has_sel))
        box.append(item('Paste', self._terminal.paste_clipboard))
        box.append(item('Select All', self._terminal.select_all))

        popover.set_child(box)
        popover.popup()

    def _set_clipboard(self, text):
        Gdk.Display.get_default().get_clipboard().set(text)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()  # fires close-request → _on_close_request handles cleanup
            return True
        return False

    def _on_close_request(self, window):
        if self._closing:
            return True
        self._closing = True
        self._kill_child()
        # Clear the singleton reference from Python before GTK touches anything.
        # This is the only reliable way — the GTK 'destroy' signal does not fire
        # consistently in GTK4 when the window is closed via internal machinery.
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
        GLib.idle_add(self._do_destroy)
        return True  # prevent Adw.Window default (which may hide instead of destroy)

    def _do_destroy(self):
        self.destroy()
        return GLib.SOURCE_REMOVE

    def _on_destroy(self, window):
        """Fallback cleanup if destroyed without going through close-request
        (e.g. parent window destroyed us directly)."""
        self._kill_child()
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()

    def _kill_child(self):
        if self._child_pid is not None:
            pid = self._child_pid
            self._child_pid = None
            for p in (-pid, pid):
                try:
                    os.kill(p, signal.SIGHUP)
                except (ProcessLookupError, OSError):
                    pass
        else:
            self._spawn_cancelled = True
