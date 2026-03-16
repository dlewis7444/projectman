import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from settings import Settings


class SettingsWindow(Adw.PreferencesDialog):
    def __init__(self, settings, app, parent):
        super().__init__()
        self._settings = settings
        self._app = app
        self.set_title('Settings')
        self._build_general_page()
        self._build_terminal_page()
        self._build_appearance_page()
        self.present(parent)

    # ------------------------------------------------------------------ #
    #  Pages                                                               #
    # ------------------------------------------------------------------ #

    def _build_general_page(self):
        page = Adw.PreferencesPage(
            title='General', icon_name='preferences-system-symbolic'
        )
        self.add(page)

        # Group: Projects
        projects_group = Adw.PreferencesGroup(title='Projects')
        page.add(projects_group)

        self._projects_dir_row = Adw.ActionRow(title='Projects Directory')
        self._projects_dir_row.set_subtitle(self._settings.resolved_projects_dir)
        choose_btn = Gtk.Button(label='Choose Folder\u2026')
        choose_btn.set_valign(Gtk.Align.CENTER)
        choose_btn.add_css_class('flat')
        choose_btn.connect('clicked', self._on_choose_folder)
        self._projects_dir_row.add_suffix(choose_btn)
        self._projects_dir_row.set_activatable_widget(choose_btn)
        projects_group.add(self._projects_dir_row)

        self._claude_binary_row = Adw.EntryRow(title='Claude Binary')
        self._claude_binary_row.set_text(self._settings.claude_binary)
        self._claude_binary_row.set_show_apply_button(True)
        self._claude_binary_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        # Adw.EntryRow implements Gtk.Editable; set placeholder via GObject property
        self._claude_binary_row.set_property('placeholder-text', 'claude  (PATH default)')
        self._claude_binary_row.connect('apply', self._on_claude_binary_apply)
        projects_group.add(self._claude_binary_row)

        # Group: Startup
        startup_group = Adw.PreferencesGroup(title='Startup')
        page.add(startup_group)

        self._resume_row = Adw.SwitchRow(
            title='Resume Last Project',
            subtitle='Auto-open the last active project on launch',
        )
        self._resume_row.set_active(self._settings.resume_last_project)
        self._resume_row.connect('notify::active', self._on_resume_toggled)
        startup_group.add(self._resume_row)

    def _build_terminal_page(self):
        page = Adw.PreferencesPage(
            title='Terminal', icon_name='utilities-terminal-symbolic'
        )
        self.add(page)

        # Group: Font
        font_group = Adw.PreferencesGroup(title='Font')
        page.add(font_group)

        self._font_size_row = Adw.SpinRow.new_with_range(6, 36, 1)
        self._font_size_row.set_title('Font Size')
        self._font_size_row.set_value(self._settings.font_size)
        self._font_size_row.connect('notify::value', self._on_font_size_changed)
        font_group.add(self._font_size_row)

        # Group: Behavior
        behavior_group = Adw.PreferencesGroup(title='Behavior')
        page.add(behavior_group)

        self._scrollback_row = Adw.SpinRow.new_with_range(1000, 100000, 1000)
        self._scrollback_row.set_title('Scrollback Lines')
        self._scrollback_row.set_subtitle('Lines of terminal history to keep')
        self._scrollback_row.set_value(self._settings.scrollback_lines)
        self._scrollback_row.connect('notify::value', self._on_scrollback_changed)
        behavior_group.add(self._scrollback_row)

        self._bell_row = Adw.SwitchRow(title='Audible Bell')
        self._bell_row.set_active(self._settings.audible_bell)
        self._bell_row.connect('notify::active', self._on_bell_toggled)
        behavior_group.add(self._bell_row)

        self._multiplexer_row = Adw.ComboRow(title='Multiplexer')
        mux_options = ['zellij', 'tmux', 'screen']
        self._multiplexer_row.set_model(Gtk.StringList.new(mux_options))
        selected = mux_options.index(self._settings.multiplexer) \
            if self._settings.multiplexer in mux_options else 0
        self._multiplexer_row.set_selected(selected)
        self._multiplexer_row.connect('notify::selected', self._on_multiplexer_changed)
        behavior_group.add(self._multiplexer_row)

    def _build_appearance_page(self):
        page = Adw.PreferencesPage(
            title='Appearance', icon_name='preferences-desktop-theme-symbolic'
        )
        self.add(page)

        theme_group = Adw.PreferencesGroup(title='Theme')
        page.add(theme_group)

        theme_row = Adw.ActionRow(
            title='Theme',
            subtitle='Coming in a future release',
        )
        theme_row.set_sensitive(False)
        theme_group.add(theme_row)

    # ------------------------------------------------------------------ #
    #  Handlers                                                            #
    # ------------------------------------------------------------------ #

    def _save_and_notify(self):
        self._settings.save()
        self._app.emit('settings-changed')

    def _on_choose_folder(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title('Choose Projects Folder')
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            path = folder.get_path()
            self._settings.projects_dir = path
            self._projects_dir_row.set_subtitle(self._settings.resolved_projects_dir)
            self._save_and_notify()
        except GLib.Error:
            pass  # user cancelled

    def _on_claude_binary_apply(self, row):
        self._settings.claude_binary = row.get_text().strip()
        self._save_and_notify()

    def _on_resume_toggled(self, row, _param):
        self._settings.resume_last_project = row.get_active()
        self._save_and_notify()

    def _on_font_size_changed(self, row, _param):
        self._settings.font_size = int(row.get_value())
        self._save_and_notify()

    def _on_scrollback_changed(self, row, _param):
        self._settings.scrollback_lines = int(row.get_value())
        self._save_and_notify()

    def _on_bell_toggled(self, row, _param):
        self._settings.audible_bell = row.get_active()
        self._save_and_notify()

    def _on_multiplexer_changed(self, row, _param):
        options = ['zellij', 'tmux', 'screen']
        idx = row.get_selected()
        if 0 <= idx < len(options):
            self._settings.multiplexer = options[idx]
            self._save_and_notify()
