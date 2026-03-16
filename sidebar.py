from datetime import datetime

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GLib, GObject, Gdk

from model import ResourceReader


class Sidebar(Gtk.Box):
    __gsignals__ = {
        'project-activated':    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'session-activated':    (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'project-archive':      (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-deactivate':   (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-bash':         (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-new-claude':   (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-zellij':       (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-edit-md':      (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'show-archive-window':  (GObject.SignalFlags.RUN_FIRST, None, ()),
        'show-settings':        (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, store, history, watcher):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._store = store
        self._history = history
        self._watcher = watcher
        self._rows = {}
        self._active_only = False

        header = Gtk.Label(label='PROJECTS')
        header.add_css_class('sidebar-header')
        header.set_halign(Gtk.Align.START)
        self.append(header)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._listbox = Gtk.ListBox()
        self._listbox.add_css_class('navigation-sidebar')
        self._listbox.connect('row-activated', self._on_row_activated)
        self._listbox.set_filter_func(self._filter_row)
        self._scrolled.set_child(self._listbox)
        self.append(self._scrolled)

        # "Active Only" toggle — hides inactive projects to reduce clutter
        self._active_toggle = Gtk.ToggleButton(label='Active Only')
        self._active_toggle.set_margin_start(8)
        self._active_toggle.set_margin_end(8)
        self._active_toggle.set_margin_top(4)
        self._active_toggle.set_margin_bottom(0)
        self._active_toggle.connect('toggled', self._on_active_toggled)
        self.append(self._active_toggle)

        # "Archived Projects…" opens popup window
        archive_btn = Gtk.Button(label='Archived Projects\u2026')
        archive_btn.add_css_class('flat')
        archive_btn.set_margin_start(8)
        archive_btn.set_margin_end(8)
        archive_btn.set_margin_top(2)
        archive_btn.set_margin_bottom(4)
        archive_btn.connect('clicked', lambda b: self.emit('show-archive-window'))
        self.append(archive_btn)

        self._resource_bar = ResourceBar(
            on_settings_clicked=lambda: self.emit('show-settings')
        )
        self.append(self._resource_bar)

        self._populate()

    def _filter_row(self, row):
        if not self._active_only:
            return True
        if isinstance(row, ProjectRow):
            return row._process_running
        return True

    def _populate(self):
        # Preserve process running state across rebuilds
        running_state = {path: row._process_running for path, row in self._rows.items()}

        self._rows.clear()
        while True:
            row = self._listbox.get_row_at_index(0)
            if row is None:
                break
            self._listbox.remove(row)

        for proj in self._store.load_projects():
            row = ProjectRow(proj, self._history, self._watcher)
            if proj.path in running_state:
                row._process_running = running_state[proj.path]
                row.update_status()
            row.connect('session-activated',
                        lambda r, p, sid, pp=proj.path: self.emit('session-activated', pp, sid))
            row.connect('project-archive',
                        lambda r, p=proj.path: self.emit('project-archive', p))
            row.connect('project-deactivate',
                        lambda r, p=proj.path: self.emit('project-deactivate', p))
            row.connect('project-bash',
                        lambda r, p=proj.path: self.emit('project-bash', p))
            row.connect('project-new-claude',
                        lambda r, p=proj.path: self.emit('project-new-claude', p))
            row.connect('project-zellij',
                        lambda r, p=proj.path: self.emit('project-zellij', p))
            row.connect('project-edit-md',
                        lambda r, p=proj.path: self.emit('project-edit-md', p))
            self._listbox.append(row)
            self._rows[proj.path] = row

    def _on_row_activated(self, listbox, row):
        if isinstance(row, ProjectRow):
            self.emit('project-activated', row._project.path)

    def _on_active_toggled(self, button):
        self._active_only = button.get_active()
        self._listbox.invalidate_filter()

    def refresh_status(self):
        for row in self._rows.values():
            row.update_status()

    def refresh(self):
        self._populate()

    def set_project_running(self, path, running):
        if path in self._rows:
            self._rows[path].set_process_running(running)
            if self._active_only:
                self._listbox.invalidate_filter()

    def start_polling(self):
        self._resource_bar.start_polling()


class ProjectRow(Gtk.ListBoxRow):
    __gsignals__ = {
        'session-activated':  (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'project-archive':    (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-deactivate': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-bash':       (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-new-claude': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-zellij':     (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-edit-md':    (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, project, history, watcher):
        super().__init__()
        self._project = project
        self._history = history
        self._watcher = watcher
        self._expanded = False
        self._sessions_loaded = False
        self._process_running = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.set_margin_start(8)
        top.set_margin_end(8)
        top.set_margin_top(4)
        top.set_margin_bottom(4)

        self._status_dot = Gtk.Box()
        self._status_dot.add_css_class('status-dot')
        self._status_dot.add_css_class('status-stopped')
        top.append(self._status_dot)

        self._arrow = Gtk.Button.new_from_icon_name('pan-end-symbolic')
        self._arrow.add_css_class('flat')
        self._arrow.set_valign(Gtk.Align.CENTER)
        self._arrow.connect('clicked', self._on_expand_clicked)
        top.append(self._arrow)

        label = Gtk.Label(label=project.name)
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        top.append(label)

        outer.append(top)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._session_listbox = Gtk.ListBox()
        self._session_listbox.add_css_class('navigation-sidebar')
        self._session_listbox.connect('row-activated', self._on_session_activated)
        self._revealer.set_child(self._session_listbox)
        outer.append(self._revealer)

        self._setup_context_menu()

    def _setup_context_menu(self):
        menu = Gio.Menu()
        menu.append('New Claude Session', 'row.new-claude')
        menu.append('Deactivate',         'row.deactivate')
        menu.append('Open Bash',          'row.bash')
        menu.append('Open in Multiplexer', 'row.zellij')
        menu.append('Edit CLAUDE.md',     'row.edit-md')
        menu.append('Archive',            'row.archive')

        ag = Gio.SimpleActionGroup()

        def _add(name, signal_name):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate',
                           lambda a, p, sn=signal_name: self.emit(sn))
            ag.add_action(action)
            return action

        _add('new-claude', 'project-new-claude')
        self._deactivate_action = _add('deactivate', 'project-deactivate')
        self._deactivate_action.set_enabled(False)  # enabled only when process is running
        _add('bash',     'project-bash')
        _add('zellij',   'project-zellij')
        _add('edit-md',  'project-edit-md')
        _add('archive',  'project-archive')

        self.insert_action_group('row', ag)

        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)

        click = Gtk.GestureClick.new()
        click.set_button(3)
        click.connect('pressed', self._on_right_click)
        self.add_controller(click)

    def _on_right_click(self, gesture, n_press, x, y):
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _on_expand_clicked(self, button):
        self._expanded = not self._expanded
        self._revealer.set_reveal_child(self._expanded)
        icon = 'pan-down-symbolic' if self._expanded else 'pan-end-symbolic'
        self._arrow.set_icon_name(icon)
        if self._expanded and not self._sessions_loaded:
            self._load_sessions()
            self._sessions_loaded = True

    def _load_sessions(self):
        for i, sess in enumerate(self._history.get_sessions(self._project)):
            self._session_listbox.append(SessionHistoryRow(sess, is_default=(i == 0)))

    def _on_session_activated(self, listbox, row):
        if isinstance(row, SessionHistoryRow):
            self.emit('session-activated', self._project.path, row._session.session_id)

    def set_process_running(self, running):
        self._process_running = running
        self._deactivate_action.set_enabled(running)
        self.update_status()

    def update_status(self):
        for s in ('status-stopped', 'status-idle', 'status-active',
                  'status-working', 'status-notification'):
            self._status_dot.remove_css_class(s)
        if not self._process_running:
            self._status_dot.add_css_class('status-stopped')
            return
        self._status_dot.add_css_class(
            f'status-{self._watcher.get_project_status(self._project)}'
        )


class SessionHistoryRow(Gtk.ListBoxRow):
    def __init__(self, session, is_default=False):
        super().__init__()
        self._session = session
        self.add_css_class('session-history-row')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        title_text = session.title[:40] if session.title else '(untitled)'
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label=title_text)
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        title.add_css_class('session-title')
        title_row.append(title)
        if is_default:
            badge = Gtk.Label(label='\u21a9 continue')
            badge.add_css_class('session-default-badge')
            title_row.append(badge)
        box.append(title_row)

        try:
            dt = datetime.fromtimestamp(session.last_active / 1000)
            ts_text = dt.strftime('%b %d, %H:%M')
        except (ValueError, OSError):
            ts_text = ''
        ts = Gtk.Label(label=ts_text)
        ts.set_halign(Gtk.Align.START)
        ts.add_css_class('dim-label')
        ts.add_css_class('caption')
        box.append(ts)

        self.set_child(box)


class ResourceBar(Gtk.Box):
    def __init__(self, on_settings_clicked=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class('resource-bar')

        self._reader = ResourceReader()
        self._cpu_label = Gtk.Label(label='CPU: \u2014')
        self._cpu_label.set_halign(Gtk.Align.START)
        self._cpu_label.add_css_class('caption')
        self.append(self._cpu_label)

        self._ram_label = Gtk.Label(label='RAM: \u2014')
        self._ram_label.set_halign(Gtk.Align.START)
        self._ram_label.add_css_class('caption')
        self._ram_label.set_hexpand(True)
        self.append(self._ram_label)

        gear = Gtk.Button.new_from_icon_name('emblem-system-symbolic')
        gear.add_css_class('flat')
        gear.add_css_class('circular')
        gear.set_valign(Gtk.Align.CENTER)
        gear.set_tooltip_text('Settings')
        if on_settings_clicked is not None:
            gear.connect('clicked', lambda b: on_settings_clicked())
        self.append(gear)

    def start_polling(self):
        self._reader.read()
        GLib.timeout_add(3000, self._update)

    def _update(self):
        data = self._reader.read()
        self._cpu_label.set_label(f"CPU: {data['cpu_pct']:.0f}%")
        self._ram_label.set_label(
            f"RAM: {data['mem_used_gb']:.1f}/{data['mem_total_gb']:.1f} GB"
        )
        return GLib.SOURCE_CONTINUE
