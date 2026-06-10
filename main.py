import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')

import os
import signal
import subprocess
import sys
from gi.repository import Gtk, Adw, Gdk, Gio, GObject, GLib

from model import ProjectStore, HistoryReader, StatusWatcher, ProjectsWatcher
from window import AppWindow
from settings import Settings


VERSION = '1.1.1'

# Single source of truth for the real DBus application id. A test or harness
# overrides it via PM_APP_ID so it can never share identity with the user's
# live instance (incident bug c).
APP_ID = 'io.github.projectman'


class ProjectManApp(Adw.Application):
    __gsignals__ = {
        'settings-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, debug_flag=False):
        super().__init__(application_id=os.environ.get('PM_APP_ID') or APP_ID)
        self._debug_flag = debug_flag
        self.connect('startup', self._on_startup)
        self.connect('activate', self._on_activate)

    def _on_startup(self, app):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

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
        self._theme_provider = None

        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'icons'
        ))

        for name, method in [
            ('zoom-in', '_zoom_in'),
            ('zoom-out', '_zoom_out'),
            ('zoom-reset', '_zoom_reset'),
            ('open-settings', '_open_settings'),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', getattr(self, method))
            self.add_action(action)

        self.set_accels_for_action('app.zoom-in', ['<Control>equal'])
        self.set_accels_for_action('app.zoom-out', ['<Control>minus'])
        self.set_accels_for_action('app.zoom-reset', ['<Control>0'])
        # M-UX.12 (S11): keyboard route to Settings — the only way to reach it was
        # the mouse-only gear. Mirrors the zoom accels (app action + accel).
        self.set_accels_for_action('app.open-settings', ['<Control>comma'])

        # Signal-safe shutdown: SIGTERM (logout/system stop) and SIGHUP
        # (terminal hangup) must save the session and tear down direct children
        # cleanly instead of orphaning setsid process groups.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_unix_signal)

    def _on_unix_signal(self):
        """SIGTERM/SIGHUP handler: emergency shutdown, then quit. One-shot.

        Returns GLib.SOURCE_REMOVE so the source is removed after the first
        signal — a second signal arriving mid-shutdown cannot re-enter this
        handler.
        """
        window = getattr(self, '_window', None)
        if window is not None:
            window.emergency_shutdown()
        self.quit()
        return GLib.SOURCE_REMOVE

    def _on_activate(self, app):
        # A second activation (e.g. a DBus re-activate) must raise the existing
        # window, never rebuild — rebuilding spawns a duplicate window and
        # re-restores every project (incident bug a).
        if getattr(self, '_window', None) is not None:
            self._window.present()
            return
        old_dir = os.path.expanduser('~/.projectman')
        new_dir = os.path.expanduser('~/.ProjectMan')
        if not os.path.exists(new_dir) and os.path.exists(old_dir):
            try:
                os.rename(old_dir, new_dir)
                print('ProjectMan: migrated ~/.projectman → ~/.ProjectMan', file=sys.stderr)
            except OSError as e:
                print(f'ProjectMan: migration failed: {e}', file=sys.stderr)
        self._settings = Settings.load()
        self._settings.debug_logging = self._debug_flag
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
        from paa_ledger import Ledger
        from paa_monitor import PAAMonitor
        self._paa_ledger = Ledger()
        self._paa_ledger.load()
        self._paa_monitor = PAAMonitor(self._store, self._paa_ledger, self._settings)
        self._window = AppWindow(
            self, self._store, self._history, self._watcher,
            self._settings, self._zellij_watcher, version=VERSION,
            paa_ledger=self._paa_ledger, paa_monitor=self._paa_monitor,
        )
        self._load_theme_css()
        self._projects_watcher.connect('projects-changed', self._on_projects_changed)
        self.connect('settings-changed', self._on_settings_changed)
        self._window.present()
        # Bring ccr up before restoring sessions so custom-model terminals find
        # the gateway already listening. save() persists a freshly minted key.
        try:
            import ccr
            if ccr.available(self._settings) and self._settings.any_custom_model_active():
                ccr.sync(self._settings)
                self._settings.save()
        except Exception as e:
            print(f'ProjectMan: ccr sync failed: {e}', file=sys.stderr)
        self._window._restore_session()
        if self._settings.paa_enabled:
            self._paa_monitor.start()

    def _load_theme_css(self):
        display = Gdk.Display.get_default()
        if self._theme_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._theme_provider)
        self._theme_provider = Gtk.CssProvider()
        theme_name = self._settings.theme
        app_dir = os.path.dirname(os.path.abspath(__file__))
        theme_path = os.path.join(app_dir, 'themes', f'{theme_name}.css')
        if os.path.exists(theme_path):
            self._theme_provider.load_from_path(theme_path)
        Gtk.StyleContext.add_provider_for_display(
            display,
            self._theme_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

    def _on_settings_changed(self, app):
        self._load_theme_css()
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
        if hasattr(self, '_paa_monitor'):
            self._paa_monitor.restart()
        # Reconcile the ccr service (config + start/stop). sync() self-diffs,
        # so calling it on every settings change is cheap.
        try:
            import ccr
            ccr.sync(self._settings)
        except Exception as e:
            print(f'ProjectMan: ccr sync failed: {e}', file=sys.stderr)

    def _on_projects_changed(self, watcher):
        self._window._sidebar.refresh()
        self._window._sync_running_state()
        self._refresh_paa_snapshot()
        if hasattr(self, '_paa_monitor') and self._settings.paa_enabled:
            self._paa_monitor.schedule_scan()

    def _refresh_paa_snapshot(self):
        paa_dir = os.path.join(
            self._settings.resolved_projects_dir, '.project-admin-agent'
        )
        script = os.path.join(paa_dir, 'gather-context.sh')
        if os.path.exists(script):
            subprocess.Popen(
                [script], cwd=paa_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

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

    def _open_settings(self, action, param):
        """M-UX.12: Ctrl+, opens Settings via the window's existing handler
        (idempotent — re-raises an already-open Settings dialog)."""
        window = getattr(self, '_window', None)
        if window is not None:
            window._on_open_settings()

    def _get_active_terminal(self):
        if hasattr(self, '_window') and self._window._active_path:
            return self._window._terminals.get(self._window._active_path)
        return None


def main():
    debug_flag = '--debug' in sys.argv
    argv = [a for a in sys.argv if a != '--debug']
    app = ProjectManApp(debug_flag)
    app.run(argv)


if __name__ == '__main__':
    main()
