import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')

import os
import sys
from gi.repository import Gtk, Adw, Gdk, Gio, GObject

from model import ProjectStore, HistoryReader, StatusWatcher, ProjectsWatcher
from window import AppWindow
from settings import Settings


VERSION = '0.1.0'


class ProjectManApp(Adw.Application):
    __gsignals__ = {
        'settings-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(application_id='com.lewislab.ProjectMan')
        self.connect('startup', self._on_startup)
        self.connect('activate', self._on_activate)

    def _on_startup(self, app):
        provider = Gtk.CssProvider()
        css_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'style.css'
        )
        provider.load_from_path(css_path)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        for name, method in [
            ('zoom-in', '_zoom_in'),
            ('zoom-out', '_zoom_out'),
            ('zoom-reset', '_zoom_reset'),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', getattr(self, method))
            self.add_action(action)

        self.set_accels_for_action('app.zoom-in', ['<Control>equal'])
        self.set_accels_for_action('app.zoom-out', ['<Control>minus'])
        self.set_accels_for_action('app.zoom-reset', ['<Control>0'])

    def _on_activate(self, app):
        old_dir = os.path.expanduser('~/.projectman')
        new_dir = os.path.expanduser('~/.ProjectMan')
        if not os.path.exists(new_dir) and os.path.exists(old_dir):
            try:
                os.rename(old_dir, new_dir)
                print('ProjectMan: migrated ~/.projectman → ~/.ProjectMan', file=sys.stderr)
            except OSError as e:
                print(f'ProjectMan: migration failed: {e}', file=sys.stderr)
        self._settings = Settings.load()
        self._store = ProjectStore(self._settings)
        self._history = HistoryReader()
        self._history.load()
        self._watcher = StatusWatcher()
        self._watcher.start()
        from zellij import ZellijWatcher
        self._zellij_watcher = ZellijWatcher()
        if self._settings.multiplexer == 'zellij':
            self._zellij_watcher.start()
        self._projects_watcher = ProjectsWatcher()
        self._projects_watcher.start(self._settings.resolved_projects_dir)
        self._last_projects_dir = self._settings.resolved_projects_dir
        self._window = AppWindow(
            self, self._store, self._history, self._watcher,
            self._settings, self._zellij_watcher
        )
        self._projects_watcher.connect('projects-changed', self._on_projects_changed)
        self.connect('settings-changed', self._on_settings_changed)
        self._window.present()
        self._window._restore_session()

    def _on_settings_changed(self, app):
        self._window.apply_settings(self._settings)
        if self._settings.multiplexer == 'zellij':
            self._zellij_watcher.start()
        else:
            self._zellij_watcher.stop()
        new_dir = self._settings.resolved_projects_dir
        if new_dir != self._last_projects_dir:
            self._projects_watcher.restart(new_dir)
            self._window._sidebar.refresh()
            self._window._sync_running_state()
            self._last_projects_dir = new_dir

    def _on_projects_changed(self, watcher):
        self._window._sidebar.refresh()
        self._window._sync_running_state()

    def _zoom_in(self, action, param):
        tv = self._get_active_terminal()
        if tv:
            tv.zoom_in()

    def _zoom_out(self, action, param):
        tv = self._get_active_terminal()
        if tv:
            tv.zoom_out()

    def _zoom_reset(self, action, param):
        tv = self._get_active_terminal()
        if tv:
            tv.zoom_reset()

    def _get_active_terminal(self):
        if hasattr(self, '_window') and self._window._active_path:
            return self._window._terminals.get(self._window._active_path)
        return None


def main():
    app = ProjectManApp()
    app.run(sys.argv)


if __name__ == '__main__':
    main()
