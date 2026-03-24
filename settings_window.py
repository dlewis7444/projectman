import os

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
        self._build_paa_page()
        self._build_appearance_page()
        self._build_about_page()
        self._build_claude_json_page()
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
        self._claude_binary_row.set_tooltip_text('Leave blank to use "claude" from PATH')
        self._claude_binary_row.connect('apply', self._on_claude_binary_apply)
        projects_group.add(self._claude_binary_row)

        # Group: Startup
        startup_group = Adw.PreferencesGroup(title='Startup')
        page.add(startup_group)

        self._resume_row = Adw.SwitchRow(
            title='Resume projects on startup',
            subtitle='Restore all active projects from the last session',
        )
        self._resume_row.set_active(self._settings.resume_projects)
        self._resume_row.connect('notify::active', self._on_resume_toggled)
        startup_group.add(self._resume_row)

        # Group: Developer
        dev_group = Adw.PreferencesGroup(title='Developer')
        page.add(dev_group)

        self._debug_row = Adw.SwitchRow(
            title='Debug Logging',
            subtitle='Print debug output to stdout (also enabled by --debug flag)',
        )
        self._debug_row.set_active(self._settings.debug_logging)
        self._debug_row.connect('notify::active', self._on_debug_toggled)
        dev_group.add(self._debug_row)

        # Group: Notifications
        notif_group = Adw.PreferencesGroup(title='Notifications')
        page.add(notif_group)

        self._ntfy_row = Adw.SwitchRow(
            title='Enable ntfy.sh notifications',
            subtitle='May need to authorize in your ntfy.sh account',
        )
        self._ntfy_row.set_active(self._settings.ntfy_enabled)
        self._ntfy_row.connect('notify::active', self._on_ntfy_toggled)
        notif_group.add(self._ntfy_row)

        self._ntfy_topic_row = Adw.EntryRow(title='Topic')
        self._ntfy_topic_row.set_text(self._settings.ntfy_topic)
        self._ntfy_topic_row.set_show_apply_button(True)
        self._ntfy_topic_row.set_sensitive(self._settings.ntfy_enabled)
        self._ntfy_topic_row.connect('apply', self._on_ntfy_topic_apply)
        notif_group.add(self._ntfy_topic_row)

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
        mux_options = ['none', 'zellij', 'tmux', 'screen']
        self._mux_labels = ['None (direct)', 'Zellij', 'Tmux', 'Screen']
        self._multiplexer_row.set_model(Gtk.StringList.new(self._mux_labels))
        selected = mux_options.index(self._settings.multiplexer) \
            if self._settings.multiplexer in mux_options else 0
        self._multiplexer_row.set_selected(selected)
        self._multiplexer_row.connect('notify::selected', self._on_multiplexer_changed)
        behavior_group.add(self._multiplexer_row)

    def _build_paa_page(self):
        page = Adw.PreferencesPage(
            title='PAA', icon_name='applications-system-symbolic'
        )
        self.add(page)

        # -- Enable group --
        enable_group = Adw.PreferencesGroup(
            title='Projects Admin Agent',
            description='Proactive background monitor for project health',
        )
        page.add(enable_group)

        self._paa_enabled_row = Adw.SwitchRow(
            title='Enable PAA',
            subtitle='Scans projects for issues (filesystem only — no API cost)',
        )
        self._paa_enabled_row.set_active(self._settings.paa_enabled)
        self._paa_enabled_row.connect('notify::active', self._on_paa_enabled_toggled)
        enable_group.add(self._paa_enabled_row)

        self._paa_interval_row = Adw.ActionRow(
            title='Scan Interval',
            subtitle='How often PAA checks projects for issues',
        )
        self._paa_interval_row.set_sensitive(self._settings.paa_enabled)
        self._paa_interval_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 5, 120, 5
        )
        self._paa_interval_scale.set_draw_value(True)
        self._paa_interval_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self._paa_interval_scale.set_value(self._settings.paa_loop_interval_minutes)
        self._paa_interval_scale.set_size_request(200, -1)
        self._paa_interval_scale.set_valign(Gtk.Align.CENTER)
        self._paa_interval_scale.add_mark(5, Gtk.PositionType.BOTTOM, '5m')
        self._paa_interval_scale.add_mark(30, Gtk.PositionType.BOTTOM, '30m')
        self._paa_interval_scale.add_mark(60, Gtk.PositionType.BOTTOM, '1h')
        self._paa_interval_scale.add_mark(120, Gtk.PositionType.BOTTOM, '2h')
        self._paa_interval_scale.connect('value-changed', self._on_paa_interval_changed)
        self._paa_interval_row.add_suffix(self._paa_interval_scale)
        enable_group.add(self._paa_interval_row)

        # -- Budget group (Phase 2) --
        budget_group = Adw.PreferencesGroup(
            title='Token Budget',
            description='AI-powered analysis controls (Phase 2 — not yet active)',
        )
        page.add(budget_group)

        self._paa_unlimited_row = Adw.SwitchRow(
            title='Unlimited Budget',
            subtitle='Warning: removes cost guardrails for AI analysis',
        )
        self._paa_unlimited_row.set_active(self._settings.paa_budget_unlimited)
        self._paa_unlimited_row.set_sensitive(False)  # Phase 2
        budget_group.add(self._paa_unlimited_row)

        self._paa_budget_row = Adw.ActionRow(
            title='Monthly Token Budget',
            subtitle='~$0.03 per 100K tokens at Haiku rates',
        )
        self._paa_budget_row.set_sensitive(False)  # Phase 2
        self._paa_budget_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 10000, 1000000, 10000
        )
        self._paa_budget_scale.set_draw_value(True)
        self._paa_budget_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self._paa_budget_scale.set_value(self._settings.paa_budget_tokens)
        self._paa_budget_scale.set_size_request(200, -1)
        self._paa_budget_scale.set_valign(Gtk.Align.CENTER)
        self._paa_budget_scale.add_mark(100000, Gtk.PositionType.BOTTOM, '100K')
        self._paa_budget_scale.add_mark(500000, Gtk.PositionType.BOTTOM, '500K')
        self._paa_budget_scale.add_mark(1000000, Gtk.PositionType.BOTTOM, '1M')
        self._paa_budget_row.add_suffix(self._paa_budget_scale)
        budget_group.add(self._paa_budget_row)

        # -- AI Analysis group (Phase 2) --
        ai_group = Adw.PreferencesGroup(
            title='AI Analysis',
            description='These controls take effect when AI-powered checks are added',
        )
        page.add(ai_group)

        self._paa_haiku_row = Adw.SwitchRow(
            title='Use Haiku for Triage',
            subtitle='Cheaper model for initial analysis passes',
        )
        self._paa_haiku_row.set_active(self._settings.paa_allow_haiku)
        self._paa_haiku_row.set_sensitive(False)  # Phase 2
        ai_group.add(self._paa_haiku_row)

        self._paa_autonomy_row = Adw.ComboRow(title='Autonomy Level')
        self._paa_autonomy_row.set_model(Gtk.StringList.new([
            'Suggest Only', 'Auto-apply Safe Fixes', 'Full Autonomy',
        ]))
        _autonomy_options = ['suggest', 'auto-safe', 'full']
        idx = _autonomy_options.index(self._settings.paa_autonomy_level) \
            if self._settings.paa_autonomy_level in _autonomy_options else 0
        self._paa_autonomy_row.set_selected(idx)
        self._paa_autonomy_row.set_sensitive(False)  # Phase 2
        ai_group.add(self._paa_autonomy_row)

    def _build_appearance_page(self):
        page = Adw.PreferencesPage(
            title='Appearance', icon_name='preferences-desktop-theme-symbolic'
        )
        self.add(page)

        # Theme group
        theme_group = Adw.PreferencesGroup(title='Theme')
        page.add(theme_group)

        app_dir = os.path.dirname(os.path.abspath(__file__))
        themes_dir = os.path.join(app_dir, 'themes')
        _THEME_LABELS = {'argonaut': 'Argonaut Dark', 'candyland': 'Candyland', 'phosphor': 'Phosphor (Green CRT)', 'salt-spray': 'Salt Spray'}
        self._theme_names = []
        theme_labels = []
        if os.path.isdir(themes_dir):
            for fname in sorted(os.listdir(themes_dir)):
                if fname.endswith('.css'):
                    name = fname[:-4]
                    self._theme_names.append(name)
                    theme_labels.append(_THEME_LABELS.get(name, name.title()))

        self._theme_row = Adw.ComboRow(title='Color Theme')
        self._theme_row.set_model(Gtk.StringList.new(theme_labels))
        current = self._settings.theme
        if current in self._theme_names:
            self._theme_row.set_selected(self._theme_names.index(current))
        self._theme_row.connect('notify::selected', self._on_theme_changed)
        theme_group.add(self._theme_row)

        # Hook Script group
        hook_group = Adw.PreferencesGroup(title='Hook Script')
        page.add(hook_group)

        hook_path = os.path.expanduser('~/.claude/projectman/hook.js')
        self._hook_row = Adw.ActionRow(title='Hook Script')
        self._hook_row.set_subtitle(hook_path)
        edit_hook_btn = Gtk.Button(label='Edit\u2026')
        edit_hook_btn.set_valign(Gtk.Align.CENTER)
        edit_hook_btn.add_css_class('flat')
        edit_hook_btn.connect('clicked', self._on_edit_hook)
        self._hook_row.add_suffix(edit_hook_btn)
        hook_group.add(self._hook_row)

        # Status Colors group (read-only reference)
        colors_group = Adw.PreferencesGroup(title='Status Colors')
        page.add(colors_group)

        status_colors = [
            ('stopped',  'Stopped',  'alpha(#fce4f7, 0.08)'),
            ('idle',     'Idle',     'alpha(#fce4f7, 0.25)'),
            ('done',     'Done',     '#ff6eb4 (hot pink)'),
            ('working',  'Working',  '#ffaa6e (peach)'),
            ('waiting',  'Waiting',  '#c084fc (lavender)'),
        ]
        for _key, label, color in status_colors:
            row = Adw.ActionRow(title=label)
            row.set_subtitle(color)
            row.set_sensitive(False)
            colors_group.add(row)

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
        self._settings.resume_projects = row.get_active()
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

    def _on_theme_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._theme_names):
            self._settings.theme = self._theme_names[idx]
            self._save_and_notify()

    def _on_debug_toggled(self, row, _param):
        self._settings.debug_logging = row.get_active()
        if self._settings.debug_logging:
            print('[DBG] debug logging enabled', flush=True)
        self._save_and_notify()

    def _on_ntfy_toggled(self, row, _param):
        self._settings.ntfy_enabled = row.get_active()
        self._ntfy_topic_row.set_sensitive(self._settings.ntfy_enabled)
        self._save_and_notify()

    def _on_ntfy_topic_apply(self, row):
        self._settings.ntfy_topic = row.get_text().strip()
        self._save_and_notify()

    def _on_paa_enabled_toggled(self, row, _param):
        self._settings.paa_enabled = row.get_active()
        self._paa_interval_row.set_sensitive(row.get_active())
        self._save_and_notify()

    def _on_paa_interval_changed(self, scale):
        self._settings.paa_loop_interval_minutes = int(scale.get_value())
        self._save_and_notify()

    def _on_multiplexer_changed(self, row, _param):
        options = ['none', 'zellij', 'tmux', 'screen']
        idx = row.get_selected()
        if 0 <= idx < len(options):
            self._settings.multiplexer = options[idx]
            self._save_and_notify()

    def _on_edit_hook(self, button):
        hook_path = os.path.expanduser('~/.claude/projectman/hook.js')
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        try:
            with open(hook_path, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            content = ''

        dialog = Adw.Dialog()
        dialog.set_title('Edit Hook Script')
        dialog.set_content_width(600)
        dialog.set_content_height(400)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        tv = Gtk.TextView()
        tv.set_monospace(True)
        tv.get_buffer().set_text(content)
        scrolled.set_child(tv)
        toolbar_view.set_content(scrolled)

        save_btn = Gtk.Button(label='Save')
        save_btn.add_css_class('suggested-action')
        header.pack_end(save_btn)

        def _save(btn):
            buf = tv.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
            try:
                with open(hook_path, 'w') as hf:
                    hf.write(text)
                dialog.close()
            except OSError as e:
                pass  # TODO: show error toast

        save_btn.connect('clicked', _save)
        dialog.set_child(toolbar_view)
        dialog.present(self)

    def _on_save_claude_json(self, button):
        json_path = os.path.expanduser('~/.claude/settings.json')
        buf = self._claude_json_tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        try:
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
            with open(json_path, 'w') as f:
                f.write(text)
            toast = Adw.Toast.new('Saved successfully')
            toast.set_timeout(2)
            self.add_toast(toast)
        except OSError as e:
            toast = Adw.Toast.new(f'Error saving: {e}')
            toast.set_timeout(4)
            self.add_toast(toast)

    # ------------------------------------------------------------------ #
    #  Extra Pages                                                         #
    # ------------------------------------------------------------------ #

    def _build_about_page(self):
        from main import VERSION
        page = Adw.PreferencesPage(
            title='About', icon_name='help-about-symbolic'
        )
        self.add(page)

        info_group = Adw.PreferencesGroup()
        page.add(info_group)

        # Try to load ProjectMan.jpg
        app_dir = os.path.dirname(os.path.abspath(__file__))
        jpg_path = os.path.join(app_dir, 'ProjectMan.jpg')
        if os.path.exists(jpg_path):
            picture = Gtk.Picture.new_for_filename(jpg_path)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_size_request(200, -1)
            picture.set_halign(Gtk.Align.CENTER)
            picture.set_margin_bottom(12)
            info_group.add(picture)

        name_row = Adw.ActionRow(title='ProjectMan')
        name_row.set_subtitle(f'Version {VERSION}')
        name_row.set_sensitive(False)
        info_group.add(name_row)

        desc_row = Adw.ActionRow(title='Description')
        desc_row.set_subtitle('GTK4 desktop manager for Claude Code sessions')
        desc_row.set_sensitive(False)
        info_group.add(desc_row)

        license_row = Adw.ActionRow(title='License')
        license_row.set_subtitle('MIT')
        license_row.set_sensitive(False)
        info_group.add(license_row)

    def _build_claude_json_page(self):
        page = Adw.PreferencesPage(
            title='Claude JSON', icon_name='text-editor-symbolic'
        )
        self.add(page)

        json_path = os.path.expanduser('~/.claude/settings.json')
        try:
            with open(json_path, 'r') as f:
                json_content = f.read()
        except FileNotFoundError:
            json_content = '{}'

        group = Adw.PreferencesGroup(title='~/.claude/settings.json')
        page.add(group)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(300)
        self._claude_json_tv = Gtk.TextView()
        self._claude_json_tv.set_monospace(True)
        self._claude_json_tv.set_left_margin(8)
        self._claude_json_tv.set_right_margin(8)
        self._claude_json_tv.set_top_margin(8)
        self._claude_json_tv.set_bottom_margin(8)
        self._claude_json_tv.get_buffer().set_text(json_content)
        scrolled.set_child(self._claude_json_tv)
        group.add(scrolled)

        save_row = Adw.ActionRow()
        save_btn = Gtk.Button(label='Save')
        save_btn.add_css_class('suggested-action')
        save_btn.set_valign(Gtk.Align.CENTER)
        save_btn.connect('clicked', self._on_save_claude_json)
        save_row.add_suffix(save_btn)
        page.add(Adw.PreferencesGroup())  # spacer
        # Add button row directly
        btn_group = Adw.PreferencesGroup()
        page.add(btn_group)
        btn_group.add(save_row)
