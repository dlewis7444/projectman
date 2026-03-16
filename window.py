import os
import subprocess

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from sidebar import Sidebar
from terminal import TerminalView
from archive_window import ArchiveWindow
from shutdown_window import ShutdownWindow
from model import Project


class AppWindow(Adw.ApplicationWindow):
    def __init__(self, app, store, history, watcher, settings):
        super().__init__(application=app)
        self._store = store
        self._history = history
        self._watcher = watcher
        self._settings = settings
        self._terminals = {}
        self._active_path = None
        self._archive_win = None
        self._settings_win = None

        self.set_default_size(1200, 750)
        self.set_title('ProjectMan')

        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title='ProjectMan', subtitle='')
        self._header.set_title_widget(self._title)
        toolbar_view.add_top_bar(self._header)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(220)
        self._paned.set_resize_start_child(False)
        self._paned.set_shrink_start_child(False)

        self._sidebar = Sidebar(store, history, watcher)
        self._sidebar.connect('project-activated',   self._on_project_activated)
        self._sidebar.connect('session-activated',   self._on_session_activated)
        self._sidebar.connect('project-archive',     self._on_project_archive)
        self._sidebar.connect('project-deactivate',  self._on_project_deactivate)
        self._sidebar.connect('project-new-claude',  self._on_project_new_claude)
        self._sidebar.connect('project-zellij',      self._on_project_open_multiplexer)
        self._sidebar.connect('project-edit-md',     self._on_project_edit_md)
        self._sidebar.connect('show-archive-window', self._on_show_archive_window)
        self._sidebar.connect('show-settings',       self._on_open_settings)
        self._sidebar.connect('project-create', self._on_project_create)
        self._sidebar.connect('project-rename', self._on_project_rename)
        self._paned.set_start_child(self._sidebar)

        self._stack = Gtk.Stack()
        placeholder = Adw.StatusPage()
        placeholder.set_title('Select a Project')
        placeholder.set_description(
            'Click a project in the sidebar to start a Claude session'
        )
        placeholder.set_icon_name('folder-symbolic')
        self._stack.add_named(placeholder, '__placeholder__')
        self._paned.set_end_child(self._stack)

        toolbar_view.set_content(self._paned)
        self.set_content(toolbar_view)

        watcher.connect('status-changed', lambda w: self._sidebar.refresh_status())
        self.connect('close-request', self._on_close_request)
        self._sidebar.start_polling()
        self._setup_shortcuts()

    def _on_close_request(self, window):
        running = {path: tv for path, tv in self._terminals.items()
                   if tv._child_pid is not None}
        if not running:
            return False  # nothing active, close immediately

        # If any session is actively working (orange dot), confirm first
        working_names = [
            self._find_project(p).name
            for p in running
            if (proj := self._find_project(p)) and
               self._watcher.get_project_status(proj) == 'working'
        ]
        if working_names:
            self._show_working_confirm(running, working_names)
        else:
            self._open_shutdown_window(running)
        return True  # prevent immediate close — shutdown window drives the close

    def _show_working_confirm(self, running, working_names):
        names_str = '\n'.join(f'\u2022 {n}' for n in working_names)
        dialog = Adw.AlertDialog.new(
            'Interrupt Active Work?',
            f'Claude is currently working on:\n{names_str}\n\n'
            f'Closing ProjectMan may interrupt incomplete operations.',
        )
        dialog.add_response('cancel', 'Keep Running')
        dialog.add_response('close', 'Close Anyway')
        dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def on_response(d, response_id):
            if response_id == 'close':
                self._open_shutdown_window(running)

        dialog.connect('response', on_response)
        dialog.present(self)

    def _open_shutdown_window(self, running):
        ShutdownWindow(parent=self, running=running, on_complete=self.destroy)

    def _setup_shortcuts(self):
        controller = Gtk.ShortcutController.new()
        controller.set_scope(Gtk.ShortcutScope.MANAGED)
        controller.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string('F5'),
            Gtk.CallbackAction.new(self._on_f5),
        ))
        self.add_controller(controller)

    def _on_f5(self, widget, args):
        if self._active_path and self._active_path in self._terminals:
            project = self._find_project(self._active_path)
            pname = project.name if project else None
            self._terminals[self._active_path].spawn_claude(project_name=pname)
        return True

    def _get_or_create_terminal(self, project):
        if project.path not in self._terminals:
            tv = TerminalView(project, self._settings)
            tv.connect('process-started',
                       lambda t, p=project.path: self._sidebar.set_project_running(p, True))
            tv.connect('process-exited',
                       lambda t, s, p=project.path: self._sidebar.set_project_running(p, False))
            self._terminals[project.path] = tv
            self._stack.add_named(tv, project.path)
        return self._terminals[project.path]

    def _sync_running_state(self):
        """Re-apply process running flags after a sidebar refresh."""
        for path, tv in self._terminals.items():
            if tv._child_pid is not None:
                self._sidebar.set_project_running(path, True)

    def _find_project(self, path):
        for p in self._store.load_projects() + self._store.load_archived():
            if p.path == path:
                return p
        return None

    # --- project activation ---

    def _on_project_activated(self, sidebar, path):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        if tv._child_pid is None:
            tv.spawn_claude(project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_session_activated(self, sidebar, path, session_id):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        tv.spawn_claude(session_id=session_id, project_name=project.name)
        tv.get_terminal().grab_focus()

    # --- deactivate (kill process, keep in sidebar as inactive) ---

    def _on_project_deactivate(self, sidebar, path):
        if path in self._terminals:
            self._terminals[path].deactivate()
            # process-exited signal will fire → sidebar.set_project_running(path, False)

    # --- archive (move to .archive, remove terminal) ---

    def _on_project_archive(self, sidebar, path):
        if path in self._terminals:
            tv = self._terminals.pop(path)
            tv._kill_child()
            self._stack.remove(tv)
        project = self._find_project(path)
        if project:
            self._store.archive(project)
        self._sidebar.refresh()
        self._sync_running_state()
        if self._active_path == path:
            self._stack.set_visible_child_name('__placeholder__')
            self._active_path = None
            self._title.set_subtitle('')

    # --- archive popup ---

    def _on_show_archive_window(self, sidebar):
        if self._archive_win is not None:
            self._archive_win.present()
            return
        self._archive_win = ArchiveWindow(
            parent=self,
            store=self._store,
            on_restore=self._on_archived_project_restored,
        )
        self._archive_win.connect('destroy', lambda w: setattr(self, '_archive_win', None))
        self._archive_win.present()

    def _on_archived_project_restored(self, project):
        self._store.restore(project)
        self._sidebar.refresh()
        self._sync_running_state()

    # --- other terminal actions ---

    def _on_project_new_claude(self, sidebar, path):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        tv.spawn_claude(fresh=True, project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_project_open_multiplexer(self, sidebar, path):
        if not self._settings.multiplexer or self._settings.multiplexer == 'none':
            return
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        tv.spawn_multiplexer(self._settings.multiplexer, project.name)
        tv.get_terminal().grab_focus()

    def apply_settings(self, settings):
        """Apply updated settings to all running terminals."""
        self._settings = settings
        for tv in self._terminals.values():
            tv.apply_settings(settings)

    def _on_open_settings(self, *args):
        if self._settings_win is not None:
            self._settings_win.present(self)
            return
        from settings_window import SettingsWindow
        self._settings_win = SettingsWindow(
            self._settings, self.get_application(), self
        )
        self._settings_win.connect(
            'closed', lambda w: setattr(self, '_settings_win', None)
        )

    def _activate_last_project(self):
        """Auto-open the most recently active project (called from main after present())."""
        if not self._settings.resume_last_project:
            return
        best_project = None
        best_ts = 0
        for proj in self._store.load_projects():
            sessions = self._history.get_sessions(proj)
            if sessions and sessions[0].last_active > best_ts:
                best_ts = sessions[0].last_active
                best_project = proj
        if best_project:
            self._on_project_activated(self._sidebar, best_project.path)

    def _on_project_edit_md(self, sidebar, path):
        editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi'
        claude_md = os.path.join(path, 'CLAUDE.md')
        subprocess.Popen([editor, claude_md], cwd=path)

    def _on_project_create(self, sidebar, name):
        try:
            self._store.create_project(name)
        except OSError:
            return
        self._sidebar.refresh()

    def _on_project_rename(self, sidebar, old_path, new_name):
        project = self._find_project(old_path)
        if not project:
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        try:
            self._store.rename_project(project, new_name)
        except OSError:
            return

        # Migrate terminal stack entry so the running session survives the rename
        if old_path in self._terminals:
            tv = self._terminals.pop(old_path)
            self._stack.remove(tv)
            self._stack.add_named(tv, new_path)
            self._terminals[new_path] = tv
            tv._project = Project(name=new_name, path=new_path)

        if self._active_path == old_path:
            self._active_path = new_path
            self._title.set_subtitle(new_name)

        self._sidebar.refresh()
        self._sync_running_state()
