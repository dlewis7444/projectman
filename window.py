import os
import subprocess

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk

from sidebar import Sidebar
from terminal import TerminalView
from archive_window import ArchiveWindow
from shutdown_window import ShutdownWindow
from model import Project
from session import (
    save_session, load_session, filter_active_paths,
    collect_session_state, plan_restore, SESSION_FILE,
)


class AppWindow(Adw.ApplicationWindow):
    def __init__(self, app, store, history, watcher, settings, zellij_watcher):
        super().__init__(application=app)
        self._store = store
        self._history = history
        self._watcher = watcher
        self._settings = settings
        self._terminals = {}
        self._active_path = None
        self._mru = []          # most-recently-used project paths, index 0 = current
        self._archive_win = None
        self._settings_win = None
        self._zellij_watcher = zellij_watcher
        zellij_watcher.connect('sessions-changed', self._on_zellij_sessions_changed)

        self.set_default_size(1200, 750)
        self.set_title('ProjectMan')

        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title='ProjectMan', subtitle='')
        self._header.set_title_widget(self._title)

        sidebar_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        projects_lbl = Gtk.Label(label='PROJECTS')
        projects_lbl.add_css_class('pm-sidebar-title')
        sidebar_head.append(projects_lbl)
        self._pin_btn = Gtk.ToggleButton()
        self._pin_btn.set_active(True)
        self._pin_btn.set_icon_name('sidebar-show-symbolic')
        self._pin_btn.add_css_class('flat')
        self._pin_btn.set_tooltip_text('Pin sidebar')
        self._pin_btn.connect('toggled', self._on_sidebar_pin_toggled)
        sidebar_head.append(self._pin_btn)
        self._header.pack_start(sidebar_head)

        toolbar_view.add_top_bar(self._header)

        self._sidebar_pos = settings.sidebar_width
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(settings.sidebar_width)
        self._paned.set_resize_start_child(False)
        self._paned.set_shrink_start_child(False)
        self._paned.connect('notify::position', self._on_paned_position_notify)

        self._sidebar = Sidebar(store, history, watcher)
        self._sidebar.connect('project-activated',   self._on_project_activated)
        self._sidebar.connect('session-activated',   self._on_session_activated)
        self._sidebar.connect('project-archive',     self._on_project_archive)
        self._sidebar.connect('project-deactivate',  self._on_project_deactivate)
        self._sidebar.connect('project-new-claude',  self._on_project_new_claude)
        self._sidebar.connect('project-zellij',      self._on_project_open_zellij)
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

    def _on_zellij_sessions_changed(self, watcher):
        """A session appeared or disappeared — reconcile sidebar state."""
        if self._settings.multiplexer != 'zellij':
            return
        import zellij as z
        for project in self._store.load_projects():
            path = project.path
            sname = z.session_name(project.name)
            tv = self._terminals.get(path)
            currently_attached = tv is not None and tv._child_pid is not None
            if currently_attached:
                continue  # process-exited will handle this case
            if z.session_alive(sname):
                self._sidebar.set_project_state(path, 'detached')
            else:
                self._sidebar.set_project_state(path, 'inactive')

    def _on_close_request(self, window):
        self._settings.sidebar_width = self._sidebar_pos
        self._settings.save()
        running = {path: tv for path, tv in self._terminals.items()
                   if tv._child_pid is not None}
        if not running:
            self._save_session()      # write empty session; restore is a no-op
            return False

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
        self._save_session()      # snapshot before SIGTERM
        ShutdownWindow(parent=self, running=running, on_complete=self.destroy)

    def _save_session(self):
        """Snapshot running terminals to SESSION_FILE (atomic write)."""
        if not self._settings.resume_projects:
            return
        open_paths, focused = collect_session_state(self._terminals, self._active_path)
        save_session(SESSION_FILE, open_paths, focused)

    def _restore_session(self):
        """Restore projects that were running at the last committed close."""
        if self._settings.multiplexer == 'zellij':
            self._restore_zellij_session()
            return
        # --- direct-claude mode (original behaviour) ---
        if not self._settings.resume_projects:
            return
        open_paths, focused_path = load_session(SESSION_FILE)
        active = filter_active_paths(open_paths, self._store.load_projects())
        focused, background = plan_restore(open_paths, focused_path, active)
        self._sidebar.set_active_only(bool(active))
        if focused:
            self._on_project_activated(self._sidebar, focused)
        for path in background:
            project = active[path]
            tv = self._get_or_create_terminal(project)
            if tv._child_pid is None:
                tv.spawn_claude(project_name=project.name)

    def _restore_zellij_session(self):
        """In zellij mode: find live pm-* sessions, mark detached, re-open last-focused.

        Falls back to session.json when no live sessions exist (e.g. first run after
        switching from direct-claude mode, or after a system reboot that cleared sessions).
        _on_project_activated decides per-project whether to attach zellij or spawn claude.
        """
        import zellij as z
        alive_names = z.alive_session_names()
        live = []
        for project in self._store.load_projects():
            sname = z.session_name(project.name)
            if sname in alive_names:
                self._sidebar.set_project_state(project.path, 'detached')
                live.append(project)

        if not self._settings.resume_projects:
            self._sidebar.set_active_only(bool(live))
            return

        open_paths, focused_path = load_session(SESSION_FILE)
        all_paths = {p.path for p in self._store.load_projects()}

        restore_path = focused_path if focused_path and focused_path in all_paths else None
        if restore_path is None:
            for path in open_paths:
                if path in all_paths:
                    restore_path = path
                    break

        background = [p for p in open_paths if p != restore_path and p in all_paths]
        self._sidebar.set_active_only(bool(live) or bool(restore_path))

        if restore_path:
            self._on_project_activated(self._sidebar, restore_path)

        for path in background:
            project = self._find_project(path)
            if not project:
                continue
            tv = self._get_or_create_terminal(project)
            if tv._child_pid is None:
                sname = z.session_name(project.name)
                if sname in alive_names:
                    tv.spawn_zellij(sname)
                else:
                    tv.spawn_claude(project_name=project.name)

    def _push_mru(self, path):
        self._mru = [path] + [p for p in self._mru if p != path]

    def _setup_shortcuts(self):
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_F5:
            return self._on_f5()
        if ctrl and keyval == Gdk.KEY_Tab:
            return self._on_ctrl_tab()
        return False

    def _on_ctrl_tab(self):
        self._debug(f'ctrl+tab mru={[os.path.basename(p) for p in self._mru]}')
        if len(self._mru) >= 2:
            self._switch_to_project(self._mru[1])
        return True

    def _debug(self, msg):
        if self._settings.debug_logging:
            print(f'[DBG] {msg}', flush=True)

    def _on_sidebar_pin_toggled(self, btn):
        if btn.get_active():
            self._paned.set_shrink_start_child(False)
            self._paned.set_position(self._sidebar_pos)
        else:
            self._sidebar_pos = self._paned.get_position()
            self._paned.set_shrink_start_child(True)
            self._paned.set_position(0)

    def _on_paned_position_notify(self, paned, _param):
        if self._pin_btn.get_active():
            self._sidebar_pos = paned.get_position()

    def _on_f5(self):
        if self._active_path and self._active_path in self._terminals:
            project = self._find_project(self._active_path)
            pname = project.name if project else None
            self._terminals[self._active_path].spawn_claude(project_name=pname)
        return True

    def _get_or_create_terminal(self, project):
        if project.path not in self._terminals:
            tv = TerminalView(project, self._settings)
            tv.connect('process-started',
                       lambda t, p=project.path: self._sidebar.set_project_state(p, 'attached'))
            tv.connect('process-exited',
                       lambda t, s, p=project.path: self._sidebar.set_project_state(p, 'inactive'))
            tv.connect('process-detached',
                       lambda t, p=project.path: self._sidebar.set_project_state(p, 'detached'))
            self._terminals[project.path] = tv
            self._stack.add_named(tv, project.path)
        return self._terminals[project.path]

    def _sync_running_state(self):
        """Re-apply process running flags after a sidebar refresh."""
        for path, tv in self._terminals.items():
            if tv._child_pid is not None:
                self._sidebar.set_project_state(path, 'attached')

    def _find_project(self, path):
        for p in self._store.load_projects() + self._store.load_archived():
            if p.path == path:
                return p
        return None

    # --- project activation ---

    def _switch_to_project(self, path):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        self._push_mru(path)
        if tv._child_pid is None:
            import zellij as z
            sname = z.session_name(project.name)
            if z.session_alive(sname):
                tv.spawn_zellij(sname)
            else:
                tv.spawn_claude(project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_project_activated(self, sidebar, path):
        self._switch_to_project(path)

    def _on_session_activated(self, sidebar, path, session_id):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        self._push_mru(path)
        tv.spawn_claude(session_id=session_id, project_name=project.name)
        tv.get_terminal().grab_focus()

    # --- deactivate (kill process, keep in sidebar as inactive) ---

    def _on_project_deactivate(self, sidebar, path):
        if self._settings.multiplexer == 'zellij':
            import zellij as z
            import subprocess
            project = self._find_project(path)
            if project:
                sname = z.session_name(project.name)
                if z.session_exists(sname):
                    subprocess.run(['zellij', 'kill-session', sname],
                                   capture_output=True)
                    # Killing the session causes the `zellij attach` VTE child to exit on its own.
                    # _on_child_exited fires → session_exists returns False → process-exited emitted
                    # → set_project_state(path, 'inactive') via the signal handler.
                    # If the project was detached (no VTE child running), force the state update:
                    if path not in self._terminals or self._terminals[path]._child_pid is None:
                        self._sidebar.set_project_state(path, 'inactive')
        else:
            if path in self._terminals:
                self._terminals[path].deactivate()
                # process-exited signal fires → set_project_state(path, 'inactive')

    # --- archive (move to .archive, remove terminal) ---

    def _on_project_archive(self, sidebar, path):
        if path in self._terminals:
            tv = self._terminals.pop(path)
            tv._kill_child()
            self._stack.remove(tv)
        project = self._find_project(path)
        if project:
            if self._settings.multiplexer == 'zellij':
                import zellij as z
                import subprocess
                sname = z.session_name(project.name)
                if z.session_exists(sname):
                    subprocess.run(['zellij', 'kill-session', sname],
                                   capture_output=True)
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
        self._push_mru(path)
        tv.spawn_claude(fresh=True, project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_project_open_zellij(self, sidebar, path):
        """Explicit 'Open in Zellij' — always create/attach zellij session."""
        if self._settings.multiplexer != 'zellij':
            return
        project = self._find_project(path)
        if not project:
            return
        import zellij as z
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._title.set_subtitle(project.name)
        self._active_path = path
        self._push_mru(path)
        sname = z.session_name(project.name)
        if not (tv._child_pid is not None and tv._is_zellij):
            tv.spawn_zellij(sname)
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
            self._mru = [new_path if p == old_path else p for p in self._mru]
            self._title.set_subtitle(new_name)

        self._sidebar.refresh()
        self._sync_running_state()
