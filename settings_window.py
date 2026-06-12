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
        self._build_models_page()
        self._build_agents_page()
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

        # (The Claude binary row moved to the Agents page — every agent's
        # binary is configured there now under agents['<id>']['binary'].
        # resolved_claude_binary reads agents['claude']['binary'] first, so the
        # Agents page fully drives the claude binary; the legacy claude_binary
        # key stays honored as a fallback for older settings files.)

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
            # M-UX.4 (C2): the old "filesystem only — no API cost" lied — PAA's
            # AI scans bill Anthropic. Split the copy: the master toggle enables
            # the monitor (whose FILESYSTEM checks are free); the API cost belongs
            # to "Enable AI Scans" below, which discloses it.
            subtitle='Background project health monitor. Filesystem checks are '
                     'free; AI scans (below) use the claude CLI.',
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

        self._paa_stale_row = Adw.SpinRow.new_with_range(7, 365, 7)
        self._paa_stale_row.set_title('Stale Project Threshold')
        self._paa_stale_row.set_subtitle('Days without git commits before flagging')
        self._paa_stale_row.set_value(self._settings.paa_stale_days)
        self._paa_stale_row.set_sensitive(self._settings.paa_enabled)
        self._paa_stale_row.connect('notify::value', self._on_paa_stale_changed)
        enable_group.add(self._paa_stale_row)

        # Chat model — lives here, not in AI Analysis, because Discuss
        # sessions work whether or not background AI scans are enabled.
        _chat_models = ['sonnet', 'haiku', 'opus']
        _chat_labels = ['Sonnet', 'Haiku', 'Opus']
        self._paa_chat_model_row = Adw.ComboRow(
            title='Chat Model',
            subtitle='Default model used for Discuss sessions',
        )
        self._paa_chat_model_row.set_model(Gtk.StringList.new(_chat_labels))
        chat_idx = _chat_models.index(self._settings.paa_chat_model) \
            if self._settings.paa_chat_model in _chat_models else 0
        self._paa_chat_model_row.set_selected(chat_idx)
        self._paa_chat_model_row.set_sensitive(self._settings.paa_enabled)
        self._paa_chat_model_row.connect('notify::selected', self._on_paa_chat_model_changed)
        enable_group.add(self._paa_chat_model_row)

        # -- AI Analysis --
        ai_group = Adw.PreferencesGroup(
            title='AI Analysis',
            description='AI-powered project analysis and token budget',
        )
        page.add(ai_group)

        self._paa_haiku_row = Adw.SwitchRow(
            title='Enable AI Scans',
            # M-UX.7 (C2): the AI scans always shell out to the `claude` CLI with
            # native Anthropic credentials — NOT your default agent, NOT ccr. Say
            # so, so a grok/opencode user isn't surprised by Anthropic billing.
            subtitle='Uses the claude CLI and Anthropic credentials, '
                     'regardless of your default agent',
        )
        self._paa_haiku_row.set_active(self._settings.paa_allow_haiku)
        self._paa_haiku_row.set_sensitive(self._settings.paa_enabled)
        self._paa_haiku_row.connect('notify::active', self._on_paa_haiku_toggled)
        ai_group.add(self._paa_haiku_row)

        self._paa_unlimited_row = Adw.SwitchRow(
            title='Unlimited Budget',
            subtitle='Warning: removes cost guardrails for AI analysis',
        )
        self._paa_unlimited_row.set_active(self._settings.paa_budget_unlimited)
        self._paa_unlimited_row.set_sensitive(
            self._settings.paa_enabled and self._settings.paa_allow_haiku
        )
        self._paa_unlimited_row.connect('notify::active', self._on_paa_unlimited_toggled)
        if self._settings.paa_budget_unlimited:
            self._paa_unlimited_row.add_css_class('error')
        ai_group.add(self._paa_unlimited_row)

        self._paa_budget_row = Adw.ActionRow(
            title='Monthly Token Budget',
            subtitle='~$0.03 per 100K tokens at Haiku rates',
        )
        self._paa_budget_row.set_sensitive(
            self._settings.paa_enabled
            and self._settings.paa_allow_haiku
            and not self._settings.paa_budget_unlimited
        )
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
        self._paa_budget_scale.connect('value-changed', self._on_paa_budget_changed)
        self._paa_budget_row.add_suffix(self._paa_budget_scale)
        ai_group.add(self._paa_budget_row)

        # Scan model
        _scan_models = ['haiku', 'sonnet', 'opus']
        _scan_labels = ['Haiku', 'Sonnet', 'Opus']
        self._paa_scan_model_row = Adw.ComboRow(
            title='Scan Model',
            subtitle='Model used for background AI scans',
        )
        self._paa_scan_model_row.set_model(Gtk.StringList.new(_scan_labels))
        scan_idx = _scan_models.index(self._settings.paa_scan_model) \
            if self._settings.paa_scan_model in _scan_models else 0
        self._paa_scan_model_row.set_selected(scan_idx)
        self._paa_scan_model_row.set_sensitive(
            self._settings.paa_enabled and self._settings.paa_allow_haiku
        )
        self._paa_scan_model_row.connect('notify::selected', self._on_paa_scan_model_changed)
        ai_group.add(self._paa_scan_model_row)

        self._paa_autonomy_row = Adw.ComboRow(title='Autonomy Level')
        self._paa_autonomy_row.set_model(Gtk.StringList.new([
            'Suggest Only', 'Auto-apply Safe Fixes', 'Full Autonomy',
        ]))
        _autonomy_options = ['suggest', 'auto-safe', 'full']
        idx = _autonomy_options.index(self._settings.paa_autonomy_level) \
            if self._settings.paa_autonomy_level in _autonomy_options else 0
        self._paa_autonomy_row.set_selected(idx)
        self._paa_autonomy_row.set_sensitive(
            self._settings.paa_enabled and self._settings.paa_allow_haiku
        )
        self._paa_autonomy_row.connect('notify::selected', self._on_paa_autonomy_changed)
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
        enabled = row.get_active()
        self._settings.paa_enabled = enabled
        haiku = self._settings.paa_allow_haiku
        self._paa_interval_row.set_sensitive(enabled)
        self._paa_stale_row.set_sensitive(enabled)
        self._paa_chat_model_row.set_sensitive(enabled)
        self._paa_haiku_row.set_sensitive(enabled)
        self._paa_unlimited_row.set_sensitive(enabled and haiku)
        self._paa_budget_row.set_sensitive(
            enabled and haiku and not self._settings.paa_budget_unlimited
        )
        self._paa_scan_model_row.set_sensitive(enabled and haiku)
        self._paa_autonomy_row.set_sensitive(enabled and haiku)
        self._save_and_notify()

    def _on_paa_interval_changed(self, scale):
        self._settings.paa_loop_interval_minutes = int(scale.get_value())
        self._save_and_notify()

    def _on_paa_stale_changed(self, row, _param):
        self._settings.paa_stale_days = int(row.get_value())
        self._save_and_notify()

    def _on_paa_unlimited_toggled(self, row, _param):
        self._settings.paa_budget_unlimited = row.get_active()
        self._paa_budget_row.set_sensitive(
            self._settings.paa_enabled
            and self._settings.paa_allow_haiku
            and not row.get_active()
        )
        if row.get_active():
            self._paa_unlimited_row.add_css_class('error')
        else:
            self._paa_unlimited_row.remove_css_class('error')
        self._save_and_notify()

    def _on_paa_budget_changed(self, scale):
        self._settings.paa_budget_tokens = int(scale.get_value())
        self._save_and_notify()

    def _on_paa_haiku_toggled(self, row, _param):
        haiku = row.get_active()
        self._settings.paa_allow_haiku = haiku
        self._paa_unlimited_row.set_sensitive(self._settings.paa_enabled and haiku)
        self._paa_budget_row.set_sensitive(
            self._settings.paa_enabled and haiku
            and not self._settings.paa_budget_unlimited
        )
        self._paa_scan_model_row.set_sensitive(self._settings.paa_enabled and haiku)
        self._paa_autonomy_row.set_sensitive(self._settings.paa_enabled and haiku)
        self._save_and_notify()

    def _on_paa_scan_model_changed(self, row, _param):
        models = ['haiku', 'sonnet', 'opus']
        idx = row.get_selected()
        if 0 <= idx < len(models):
            self._settings.paa_scan_model = models[idx]
            self._save_and_notify()

    def _on_paa_chat_model_changed(self, row, _param):
        models = ['sonnet', 'haiku', 'opus']
        idx = row.get_selected()
        if 0 <= idx < len(models):
            self._settings.paa_chat_model = models[idx]
            self._save_and_notify()

    def _on_paa_autonomy_changed(self, row, _param):
        options = ['suggest', 'auto-safe', 'full']
        idx = row.get_selected()
        if 0 <= idx < len(options):
            self._settings.paa_autonomy_level = options[idx]
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
    #  Models page                                                         #
    # ------------------------------------------------------------------ #

    def _build_models_page(self):
        from models import build_model_options
        page = Adw.PreferencesPage(
            title='Models', icon_name='network-server-symbolic'
        )
        self.add(page)

        # -- Active model --
        active_group = Adw.PreferencesGroup(
            title='Active Model',
            description='Default for new sessions. Override per project '
                        'from the sidebar right-click menu.',
        )
        page.add(active_group)

        self._model_combo = Adw.ComboRow(title='Default Model')
        ids, labels = build_model_options(self._settings.providers)
        self._model_ids = ids
        self._model_combo.set_model(Gtk.StringList.new(labels))
        cur = self._settings.model_default
        self._model_combo.set_selected(ids.index(cur) if cur in ids else 0)
        self._model_combo.connect('notify::selected', self._on_model_default_changed)
        # M-UX.1 (C2): tell the truth for the EFFECTIVE default agent. The combo
        # only lists claude/ccr providers, so when grok/opencode is the default
        # agent (it picks its model from its OWN config) the combo is irrelevant
        # — show the truthful "Managed by <agent> (<path>)" subtitle and make the
        # combo insensitive rather than letting it imply it controls grok's model.
        import agent_configs
        agent_id = self._settings.agent_default or 'claude'
        if agent_configs.load_agent_config(agent_id) is not None:
            self._model_combo.set_subtitle(
                agent_configs.default_model_label(self._settings))
            self._model_combo.set_sensitive(False)
        active_group.add(self._model_combo)

        # -- Provider definitions (JSON editor) --
        prov_group = Adw.PreferencesGroup(
            title='Providers',
            description='LLM providers and models as JSON. Custom models are '
                        'reached through claude-code-router.',
        )
        page.add(prov_group)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(260)
        self._providers_tv = Gtk.TextView()
        self._providers_tv.set_monospace(True)
        self._providers_tv.set_left_margin(8)
        self._providers_tv.set_right_margin(8)
        self._providers_tv.set_top_margin(8)
        self._providers_tv.set_bottom_margin(8)
        self._providers_tv.get_buffer().set_text(self._providers_json_text())
        scrolled.set_child(self._providers_tv)
        prov_group.add(scrolled)

        btn_group = Adw.PreferencesGroup()
        page.add(btn_group)
        save_row = Adw.ActionRow(title='Provider Definitions')
        save_row.set_subtitle(
            '{"<id>": {"name", "base_url", "api_key", '
            '"models": {"<id>": {"name"}}}}'
        )
        save_btn = Gtk.Button(label='Save Providers')
        save_btn.add_css_class('suggested-action')
        save_btn.set_valign(Gtk.Align.CENTER)
        save_btn.connect('clicked', self._on_save_providers)
        save_row.add_suffix(save_btn)
        btn_group.add(save_row)

        # -- Native agent model configs (read-only, M-UX.2 / C1) --
        self._build_native_model_sections(page)

        # -- claude-code-router --
        self._build_ccr_group(page)

    def _build_native_model_sections(self, page):
        """Read-only surfacing of grok's + opencode's native model configs.

        M-UX.2 (C1 VISIBILITY): grok and opencode each decide their model from a
        config file PM never showed (grok's config.toml, opencode's
        opencode.json). This adds one read-only section per agent that has such a
        file, headed with the SOURCE PATH and an "edited in the agent's own
        config" note — the read-first ruling: PM displays, it does not edit these
        (that is P4). Defensive: a missing/garbage file shows "none found", never
        raises (the parsers guarantee it).
        """
        import agent_configs
        for agent_id, display in (('grok', 'Grok Build'), ('opencode', 'opencode')):
            cfg = agent_configs.load_agent_config(agent_id)
            if cfg is None:
                continue
            shown_path = agent_configs._display_path(cfg.source_path)
            group = Adw.PreferencesGroup(
                title=f'{display} models',
                description=(f'Read-only — edited in the agent’s own config '
                            f'({shown_path}).'),
            )
            page.add(group)

            if not cfg.exists or not cfg.models:
                empty = Adw.ActionRow(title='No models found')
                empty.set_subtitle(
                    'No model definitions in this config (or the file is absent).')
                empty.set_sensitive(False)
                group.add(empty)
                continue

            for entry in cfg.models:
                is_default = (entry.key == cfg.default_key)
                title = entry.name or entry.key
                if is_default:
                    title = f'{title}  •  default'
                row = Adw.ActionRow(title=title)
                # Subtitle: the config KEY plus the upstream model id / endpoint
                # when the config states them — the full "what runs" story.
                bits = [f'key: {entry.key}']
                if entry.model and entry.model != entry.key:
                    bits.append(f'model: {entry.model}')
                if entry.base_url:
                    bits.append(entry.base_url)
                row.set_subtitle('   '.join(bits))
                row.set_sensitive(False)
                group.add(row)

    def _build_ccr_group(self, page):
        import ccr
        import agent_configs
        # B3 (M-UX.14, C2/C3): when no custom Claude models are configured, the
        # ccr block stopped frightening users who never set one up — it collapses
        # to a single self-explaining row instead of showing service-state
        # controls for a router they don't use.
        if not agent_configs.ccr_in_use(self._settings):
            group = Adw.PreferencesGroup(
                title='Claude Code Router (ccr)',
            )
            page.add(group)
            row = Adw.ActionRow(title='Claude Code Router')
            row.set_subtitle('not in use (only needed for custom Claude models)')
            row.set_sensitive(False)
            group.add(row)
            return

        group = Adw.PreferencesGroup(
            title='Claude Code Router (ccr)',
            description='Custom models are routed through a local ccr service.',
        )
        page.add(group)

        installed = ccr.available(self._settings)
        status_row = Adw.ActionRow(title='Service')
        if not installed:
            status_row.set_subtitle('Not installed (routes custom Claude models)')
            status_row.add_prefix(
                Gtk.Image.new_from_icon_name('dialog-warning-symbolic'))
        else:
            running = ccr.is_running(self._settings)
            status_row.set_subtitle(
                'Installed — service running (routes custom Claude models)'
                if running
                else 'Installed — service stopped (routes custom Claude models)')
            status_row.add_prefix(
                Gtk.Image.new_from_icon_name('emblem-ok-symbolic'))
        status_row.set_sensitive(False)
        group.add(status_row)

        if not installed:
            cmd = 'npm install -g @musistudio/claude-code-router'
            hint_row = Adw.ActionRow(title='Install ccr')
            hint_row.set_subtitle(cmd)
            copy_btn = Gtk.Button(label='Copy')
            copy_btn.set_valign(Gtk.Align.CENTER)
            copy_btn.add_css_class('flat')
            copy_btn.connect('clicked', lambda b, c=cmd: self.get_clipboard().set(c))
            hint_row.add_suffix(copy_btn)
            group.add(hint_row)

        self._ccr_managed_row = Adw.SwitchRow(
            title='Manage ccr',
            subtitle='Let ProjectMan configure and start/stop the ccr service',
        )
        self._ccr_managed_row.set_active(self._settings.ccr_managed)
        self._ccr_managed_row.set_sensitive(installed)
        self._ccr_managed_row.connect('notify::active', self._on_ccr_managed_toggled)
        group.add(self._ccr_managed_row)

    def _providers_json_text(self):
        import json
        try:
            return json.dumps(self._settings.providers, indent=2)
        except (TypeError, ValueError):
            return '{}'

    def _refresh_model_combo(self):
        """Rebuild the default-model combo after the providers dict changes."""
        from models import build_model_options
        ids, labels = build_model_options(self._settings.providers)
        current = self._settings.model_default
        self._model_combo.handler_block_by_func(self._on_model_default_changed)
        self._model_combo.set_model(Gtk.StringList.new(labels))
        self._model_ids = ids
        self._model_combo.set_selected(ids.index(current) if current in ids else 0)
        self._model_combo.handler_unblock_by_func(self._on_model_default_changed)
        if current not in ids:
            # the stored default's provider/model was removed — fall back
            self._settings.model_default = ''

    def _on_model_default_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._model_ids):
            self._settings.model_default = self._model_ids[idx]
            self._save_and_notify()

    def _on_save_providers(self, button):
        import json
        from models import validate_providers
        buf = self._providers_tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        try:
            parsed = json.loads(text or '{}')
            validate_providers(parsed)
        except (json.JSONDecodeError, ValueError) as e:
            toast = Adw.Toast.new(f'Invalid: {e}')
            toast.set_timeout(4)
            self.add_toast(toast)
            return
        self._settings.providers = parsed
        self._providers_tv.get_buffer().set_text(json.dumps(parsed, indent=2))
        self._refresh_model_combo()
        self._save_and_notify()
        toast = Adw.Toast.new('Providers saved')
        toast.set_timeout(2)
        self.add_toast(toast)

    def _on_ccr_managed_toggled(self, row, _param):
        self._settings.ccr_managed = row.get_active()
        self._save_and_notify()

    # ------------------------------------------------------------------ #
    #  Agents page (B3 — minimal this phase; full doctor is P3)            #
    # ------------------------------------------------------------------ #

    def _build_agents_page(self):
        import agents
        import agent_configs
        page = Adw.PreferencesPage(
            title='Agents', icon_name='applications-engineering-symbolic'
        )
        self.add(page)

        default_group = Adw.PreferencesGroup(
            title='Default Agent',
            description='The coding agent used for new sessions. Override per '
                        'project from the sidebar right-click menu.',
        )
        page.add(default_group)

        self._agent_default_ids = list(agents.ADAPTERS.keys())
        labels = [agents.ADAPTERS[a].display_name for a in self._agent_default_ids]
        self._agent_default_combo = Adw.ComboRow(title='Default Agent')
        self._agent_default_combo.set_model(Gtk.StringList.new(labels))
        cur = self._settings.agent_default
        self._agent_default_combo.set_selected(
            self._agent_default_ids.index(cur) if cur in self._agent_default_ids else 0)
        self._agent_default_combo.connect(
            'notify::selected', self._on_agent_default_changed)
        default_group.add(self._agent_default_combo)

        # Per-agent config: binary path + doctor-lite check.
        self._agent_binary_rows = {}
        # M-UX.8: (row, button) per agent so the bridge state can refresh after
        # an install click without rebuilding the page.
        self._bridge_rows = {}
        for agent_id in self._agent_default_ids:
            adapter = agents.ADAPTERS[agent_id]
            group = Adw.PreferencesGroup(title=adapter.display_name)
            page.add(group)

            cfg = self._settings.agents.get(agent_id, {}) if isinstance(
                self._settings.agents, dict) else {}
            binary_row = Adw.EntryRow(title='Binary')
            binary_row.set_text((cfg.get('binary') or '') if isinstance(cfg, dict) else '')
            binary_row.set_show_apply_button(True)
            binary_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
            binary_row.set_tooltip_text(
                f'Leave blank to use "{agent_id}" from PATH')
            binary_row.connect(
                'apply', lambda r, aid=agent_id: self._on_agent_binary_apply(aid, r))
            group.add(binary_row)
            self._agent_binary_rows[agent_id] = binary_row

            # Doctor-lite: <binary> --version.
            check_row = Adw.ActionRow(title='Status')
            check_row.set_subtitle('Run a check to verify the binary')
            check_btn = Gtk.Button(label='Check')
            check_btn.set_valign(Gtk.Align.CENTER)
            check_btn.add_css_class('flat')
            check_btn.connect(
                'clicked', lambda b, aid=agent_id, r=check_row: self._on_agent_doctor(aid, r))
            check_row.add_suffix(check_btn)
            group.add(check_row)

            # B2 (M-UX.13, C1/C2): per-agent account status — "is my subscription
            # connected?" answered at a glance, presence-based (the Check button
            # above stays the live probe). Contents are never read; only the
            # token file's existence/size (or, for opencode, parsed providers).
            account_line = agent_configs.account_status_line(agent_id)
            if account_line is not None:
                account_row = Adw.ActionRow(title='Account')
                account_row.set_subtitle(account_line)
                account_row.set_sensitive(False)
                group.add(account_row)

            # B1 (M-UX.8-residual / F10, C1/C5): grok reads Claude-style hooks by
            # default, which would make Claude's hook double-fire on grok events.
            # Surface the [compat.claude] hooks state read-only (the file-driven
            # behavior was invisible — a C1 defect).
            if agent_id == 'grok':
                compat_row = Adw.ActionRow(title='Claude-hooks compat')
                compat_row.set_subtitle(agent_configs.grok_compat_hooks_line())
                compat_row.set_sensitive(False)
                group.add(compat_row)

            # Status-bridge install button (only for agents that ship one).
            if agents.agent_bridge_source(self._app_dir(), agent_id) is not None \
                    or agent_id == 'opencode':
                bridge_row = Adw.ActionRow(title='Status bridge')
                bridge_btn = Gtk.Button()
                bridge_btn.set_valign(Gtk.Align.CENTER)
                bridge_btn.add_css_class('flat')
                bridge_btn.connect(
                    'clicked', lambda b, aid=agent_id: self._on_install_bridge(aid))
                bridge_row.add_suffix(bridge_btn)
                group.add(bridge_row)
                # M-UX.8 (C5): reflect the bridge's ACTUAL installed state via the
                # F12a manifest rather than always saying "Install bridge".
                self._bridge_rows[agent_id] = (bridge_row, bridge_btn)
                self._refresh_bridge_row(agent_id)

    @staticmethod
    def _app_dir():
        return os.path.dirname(os.path.abspath(__file__))

    def _on_agent_default_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._agent_default_ids):
            self._settings.agent_default = self._agent_default_ids[idx]
            self._save_and_notify()

    def _on_agent_binary_apply(self, agent_id, row):
        agents_cfg = dict(self._settings.agents) if isinstance(
            self._settings.agents, dict) else {}
        entry = dict(agents_cfg.get(agent_id) or {}) if isinstance(
            agents_cfg.get(agent_id), dict) else {}
        value = row.get_text().strip()
        entry['binary'] = value
        agents_cfg[agent_id] = entry
        self._settings.agents = agents_cfg
        if agent_id == 'claude':
            # Keep the legacy claude_binary key in sync. Without this, clearing
            # the row would leave a stale legacy value that resolved_claude_binary
            # falls back to — the clear would silently not take effect.
            self._settings.claude_binary = value
        self._save_and_notify()

    def _on_agent_doctor(self, agent_id, row):
        import agents
        ok, detail = agents.agent_doctor(self._settings, agent_id)
        row.set_subtitle(detail or ('ok' if ok else 'check failed'))
        # Swap the prefix icon to reflect the result. The Image is created once
        # per row on first check and updated thereafter — repeated checks must
        # not stack prefix icons (Adw.ActionRow has no clear-prefixes API).
        icon = 'emblem-ok-symbolic' if ok else 'dialog-warning-symbolic'
        existing = getattr(row, '_pm_doctor_icon', None)
        if existing is None:
            existing = Gtk.Image.new_from_icon_name(icon)
            row.add_prefix(existing)
            row._pm_doctor_icon = existing
        else:
            existing.set_from_icon_name(icon)

    def _refresh_bridge_row(self, agent_id):
        """Set the bridge button label + row subtitle from the manifest state
        (M-UX.8/C5). Called at build and after every install click."""
        import agents
        entry = self._bridge_rows.get(agent_id)
        if entry is None:
            return
        row, btn = entry
        state = agents.bridge_state(self._app_dir(), agent_id)
        label, subtitle = agents.bridge_button_labels(state)
        btn.set_label(label)
        row.set_subtitle(subtitle)

    def _on_install_bridge(self, agent_id):
        import agents
        result = agents.install_agent_bridge(self._app_dir(), agent_id)
        msgs = {
            'installed': 'Status bridge installed',
            'already': 'Status bridge already up to date',
            'missing-source': 'Bridge source not found in the app directory',
            'no-bridge': 'This agent has no status bridge',
            'error': 'Failed to install the status bridge',
        }
        toast = Adw.Toast.new(msgs.get(result, result))
        toast.set_timeout(3)
        self.add_toast(toast)
        # C5: the button label must now reflect the post-install state.
        self._refresh_bridge_row(agent_id)

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
        # M-UX.5 (C2): agent-neutral — the app drives Claude Code, opencode, and
        # Grok Build, so "Claude Code sessions" lied on a grok-default install.
        desc_row.set_subtitle('GTK4 desktop cockpit for AI coding agents')
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
