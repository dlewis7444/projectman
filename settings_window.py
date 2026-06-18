import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from settings import TIERS
from models import build_provider_options, build_tier_options, apply_recommended_preset


class SettingsWindow(Adw.PreferencesDialog):
    def __init__(self, settings, app, parent):
        super().__init__()
        self._settings = settings
        self._app = app
        self.set_title('Settings')
        # Guards so programmatic set_selected() during a refresh doesn't
        # re-enter the change handlers and recurse.
        self._suppress_combos = False
        self._build_general_page()
        self._build_terminal_page()
        self._build_paa_page()
        self._build_appearance_page()
        self._build_models_page()
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
        choose_btn = Gtk.Button(label='Choose Folder…')
        choose_btn.set_valign(Gtk.Align.CENTER)
        choose_btn.add_css_class('flat')
        choose_btn.connect('clicked', self._on_choose_folder)
        self._projects_dir_row.add_suffix(choose_btn)
        self._projects_dir_row.set_activatable_widget(choose_btn)
        projects_group.add(self._projects_dir_row)

        # Group: Claude Code (the binary row, re-homed from the removed Agents
        # page — Claude Code is the sole harness, so its binary config lives on
        # General now). resolved_claude_binary reads agents['claude']['binary']
        # first, falling back to the legacy claude_binary key.
        claude_group = Adw.PreferencesGroup(title='Claude Code')
        page.add(claude_group)

        cfg = (self._settings.agents.get('claude')
               if isinstance(self._settings.agents, dict) else None)
        binary = ((cfg.get('binary') or '') if isinstance(cfg, dict)
                  else self._settings.claude_binary)
        self._claude_binary_row = Adw.EntryRow(title='Binary')
        self._claude_binary_row.set_text(binary)
        self._claude_binary_row.set_show_apply_button(True)
        self._claude_binary_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._claude_binary_row.set_tooltip_text('Leave blank to use "claude" from PATH')
        self._claude_binary_row.connect('apply', self._on_claude_binary_apply)
        claude_group.add(self._claude_binary_row)

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
            # The AI scans shell out to the `claude` CLI with native Anthropic
            # credentials — NOT your custom provider. Say so.
            subtitle='Uses the claude CLI and Anthropic credentials, '
                     'regardless of your default harness',
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
        edit_hook_btn = Gtk.Button(label='Edit…')
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
    #  Handlers (General / Terminal / PAA / Appearance)                    #
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
        """Persist the Claude Code binary path into agents['claude']['binary'].

        The legacy ``claude_binary`` key is kept in sync so a clear takes effect
        (resolved_claude_binary would otherwise fall back to a stale legacy
        value)."""
        value = row.get_text().strip()
        if not isinstance(self._settings.agents, dict):
            self._settings.agents = {}
        claude_cfg = self._settings.agents.get('claude')
        if not isinstance(claude_cfg, dict):
            claude_cfg = {}
            self._settings.agents['claude'] = claude_cfg
        claude_cfg['binary'] = value
        self._settings.claude_binary = value
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
    #  Models page (click-friendly provider/tier editor)                   #
    # ------------------------------------------------------------------ #

    def _build_models_page(self):
        page = Adw.PreferencesPage(
            title='Models', icon_name='network-server-symbolic'
        )
        self.add(page)

        intro_group = Adw.PreferencesGroup(
            title='Models',
            description='Route Claude Code at any Anthropic-compatible provider '
                        '(ollama, LiteLLM, etc.). Pick a default provider, assign '
                        'the four tiers to its models, and define providers below. '
                        'Override the provider per project from the sidebar menu. '
                        'Under Zellij the provider applies to new sessions only '
                        '(an attach inherits the server env).',
        )
        page.add(intro_group)

        # -- Active Provider --
        self._active_provider_group = Adw.PreferencesGroup(title='Active Provider')
        page.add(self._active_provider_group)
        self._provider_combo = Adw.ComboRow(title='Default Provider')
        self._provider_combo.set_subtitle(
            'Anthropic (native) uses your own Anthropic credentials')
        self._provider_combo.connect('notify::selected', self._on_default_provider_changed)
        self._active_provider_group.add(self._provider_combo)

        # One-click load of the lab's known-good localhost ollama-pool mapping.
        preset_row = Adw.ActionRow(
            title='Recommended',
            subtitle="Load the lab's localhost ollama pool — sets the default "
                     'provider and assigns all four tiers (existing providers '
                     'kept).')
        preset_btn = Gtk.Button(label='Load recommended (localhost pool)')
        preset_btn.add_css_class('suggested-action')
        preset_btn.set_valign(Gtk.Align.CENTER)
        preset_btn.connect('clicked', self._on_load_recommended_preset)
        preset_row.add_suffix(preset_btn)
        self._active_provider_group.add(preset_row)

        # -- Tier Assignments --
        self._tier_group = Adw.PreferencesGroup(
            title='Tier Assignments',
            description='Assign each Claude Code tier to a model on the active '
                        'provider. "Default" uses the provider\'s first model. '
                        'Disabled when the default is native.',
        )
        page.add(self._tier_group)
        self._tier_combos = {}
        for tier, label in (('opus', 'Opus'), ('sonnet', 'Sonnet'),
                            ('haiku', 'Haiku'), ('subagent', 'Subagent')):
            combo = Adw.ComboRow(title=label)
            combo.connect('notify::selected',
                          lambda r, _p, t=tier: self._on_tier_changed(t, r))
            self._tier_group.add(combo)
            self._tier_combos[tier] = combo

        # -- Providers --
        self._providers_group = Adw.PreferencesGroup(
            title='Providers',
            description='Define Anthropic-compatible providers and their models.',
        )
        page.add(self._providers_group)

        add_group = Adw.PreferencesGroup()
        page.add(add_group)
        add_row = Adw.ActionRow()
        add_btn = Gtk.Button(label='Add Provider')
        add_btn.add_css_class('suggested-action')
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect('clicked', self._on_add_provider)
        add_row.add_suffix(add_btn)
        add_group.add(add_row)

        self._provider_card_rows = []
        self._refresh_models_page()

    def _refresh_models_page(self):
        """Rebuild the whole Models page from settings (after any change)."""
        self._refresh_provider_combo()
        self._refresh_tier_combos()
        self._rebuild_providers_group()

    def _refresh_provider_combo(self):
        ids, labels = build_provider_options(self._settings.providers)
        self._provider_ids = ids
        cur = self._settings.model_default
        self._suppress_combos = True
        self._provider_combo.set_model(Gtk.StringList.new(labels))
        self._provider_combo.set_selected(ids.index(cur) if cur in ids else 0)
        self._suppress_combos = False
        if cur not in ids:
            # The stored default's provider was removed — fall back to native.
            self._settings.model_default = ''

    def _refresh_tier_combos(self, reset_stale=False):
        """Repopulate the four tier combos from the active provider's models.

        ``reset_stale`` scrubs any tier_models value not on the active provider
        back to '' (and persists) — used when the active provider changes.
        """
        pid = self._settings.model_default
        models = [m for m in (self._settings.providers.get(pid, {}).get('models', [])
                              if isinstance(self._settings.providers.get(pid), dict)
                              else []) if isinstance(m, str)] \
            if isinstance(self._settings.providers, dict) else []
        if reset_stale and isinstance(self._settings.tier_models, dict):
            changed = False
            for tier in TIERS:
                val = self._settings.tier_models.get(tier, '')
                if isinstance(val, str) and val and val not in models:
                    self._settings.tier_models[tier] = ''
                    changed = True
            if changed:
                self._settings.save()
        active = pid != ''
        for tier, combo in self._tier_combos.items():
            ids, labels = build_tier_options(self._settings.providers, pid)
            val = self._settings.tier_models.get(tier, '') \
                if isinstance(self._settings.tier_models, dict) else ''
            if not isinstance(val, str) or val not in ids:
                val = ''
            self._suppress_combos = True
            combo.set_model(Gtk.StringList.new(labels))
            combo.set_selected(ids.index(val) if val in ids else 0)
            combo.set_sensitive(active)
            self._suppress_combos = False

    def _rebuild_providers_group(self):
        for row in list(self._provider_card_rows):
            self._providers_group.remove(row)
        self._provider_card_rows = []
        if not isinstance(self._settings.providers, dict):
            return
        for pid in sorted(self._settings.providers):
            prov = self._settings.providers.get(pid)
            if not isinstance(prov, dict):
                continue
            card = self._build_provider_card(pid, prov)
            self._providers_group.add(card)
            self._provider_card_rows.append(card)

    def _build_provider_card(self, pid, prov):
        """An expandable card for one provider: name, base_url, api_key (peek),
        model list with remove buttons, add-model entry, remove-provider button.
        """
        name = prov.get('name') or ''
        card = Adw.ExpandableRow(title=name or pid)
        card.set_subtitle(pid)

        name_row = Adw.EntryRow(title='Name')
        name_row.set_text(name)
        name_row.set_show_apply_button(True)
        name_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        name_row.connect('apply',
                         lambda r, p=pid: self._on_provider_field(p, 'name', r))
        card.add_row(name_row)

        url_row = Adw.EntryRow(title='Base URL')
        url_row.set_text(prov.get('base_url') or '')
        url_row.set_show_apply_button(True)
        url_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        url_row.set_tooltip_text('e.g. http://localhost:11434')
        url_row.connect('apply',
                        lambda r, p=pid: self._on_provider_field(p, 'base_url', r))
        card.add_row(url_row)

        key_row = Adw.ActionRow(title='API Key')
        key = prov.get('api_key') or ''
        key_row.set_subtitle(f'••••{key[-4:]}' if key else 'Not set')
        pe = Gtk.PasswordEntry()
        pe.set_show_peek_icon(True)
        pe.set_text(key)
        pe.set_valign(Gtk.Align.CENTER)
        pe.set_size_request(220, -1)
        pe.set_tooltip_text('Sent as ANTHROPIC_AUTH_TOKEN')
        # notify::text (covers paste + typing) WITHOUT rebuilding the card, so
        # focus is preserved while editing.
        pe.connect('notify::text',
                   lambda e, _p, p=pid, r=key_row: self._on_provider_key(p, e, r))
        key_row.add_suffix(pe)
        card.add_row(key_row)

        models = prov.get('models') if isinstance(prov.get('models'), list) else []
        for mid in models:
            if not isinstance(mid, str):
                continue
            m_row = Adw.ActionRow(title=mid)
            rm = Gtk.Button.new_from_icon_name('list-remove-symbolic')
            rm.add_css_class('flat')
            rm.set_valign(Gtk.Align.CENTER)
            rm.set_tooltip_text('Remove model')
            rm.connect('clicked', lambda b, p=pid, m=mid: self._on_remove_model(p, m))
            m_row.add_suffix(rm)
            card.add_row(m_row)

        add_model_row = Adw.EntryRow(title='Add model')
        add_model_row.set_show_apply_button(True)
        add_model_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        add_model_row.set_tooltip_text('Free-text id, e.g. glm-5.2:cloud[1m]')
        add_model_row.connect('apply',
                              lambda r, p=pid: self._on_add_model(p, r))
        card.add_row(add_model_row)

        rm_row = Adw.ActionRow()
        rm_btn = Gtk.Button(label='Remove provider')
        rm_btn.add_css_class('destructive-action')
        rm_btn.set_valign(Gtk.Align.CENTER)
        rm_btn.connect('clicked', lambda b, p=pid: self._on_remove_provider(p))
        rm_row.add_suffix(rm_btn)
        card.add_row(rm_row)
        return card

    # --- Models page handlers ------------------------------------------

    def _on_default_provider_changed(self, row, _param):
        if self._suppress_combos:
            return
        idx = row.get_selected()
        ids = getattr(self, '_provider_ids', [])
        if not (0 <= idx < len(ids)):
            return
        self._settings.model_default = ids[idx]
        self._settings.save()
        self._app.emit('settings-changed')
        # Repopulate the tier combos + reset any tier value not on the new
        # provider's model list to '' (the single-base_url enforcement).
        self._refresh_tier_combos(reset_stale=True)

    def _on_tier_changed(self, tier, row):
        if self._suppress_combos:
            return
        pid = self._settings.model_default
        ids, _labels = build_tier_options(self._settings.providers, pid)
        idx = row.get_selected()
        if not isinstance(self._settings.tier_models, dict):
            self._settings.tier_models = {}
        if 0 <= idx < len(ids):
            self._settings.tier_models[tier] = ids[idx]
            self._settings.save()
            self._app.emit('settings-changed')

    def _on_provider_field(self, pid, field, row):
        prov = self._settings.providers.get(pid)
        if not isinstance(prov, dict):
            return
        prov[field] = row.get_text().strip()
        self._settings.save()
        self._app.emit('settings-changed')
        # name/base_url affect the combo + card; rebuild (apply = Enter, focus
        # has already left the entry).
        self._refresh_models_page()

    def _on_provider_key(self, pid, entry, key_row):
        prov = self._settings.providers.get(pid)
        if not isinstance(prov, dict):
            return
        key = entry.get_text()
        prov['api_key'] = key
        key_row.set_subtitle(f'••••{key[-4:]}' if key else 'Not set')
        # No card rebuild — preserve focus in the password entry while typing.
        # Save + notify (the env for a running session changes on next spawn).
        self._settings.save()
        self._app.emit('settings-changed')

    def _on_add_model(self, pid, row):
        mid = row.get_text().strip()
        if not mid:
            return
        prov = self._settings.providers.get(pid)
        if not isinstance(prov, dict):
            return
        raw = prov.get('models')
        models = raw if isinstance(raw, list) else []
        if mid not in models:
            prov['models'] = list(models) + [mid]
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_models_page()

    def _on_remove_model(self, pid, mid):
        prov = self._settings.providers.get(pid)
        if not isinstance(prov, dict):
            return
        prov['models'] = [m for m in prov.get('models', []) if m != mid]
        # If this is the active provider, drop any tier pinning the removed model.
        if self._settings.model_default == pid and isinstance(self._settings.tier_models, dict):
            for tier in TIERS:
                if self._settings.tier_models.get(tier) == mid:
                    self._settings.tier_models[tier] = ''
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_models_page()

    def _on_remove_provider(self, pid):
        if isinstance(self._settings.providers, dict):
            self._settings.providers.pop(pid, None)
        if self._settings.model_default == pid:
            self._settings.model_default = ''
        if isinstance(self._settings.model_overrides, dict):
            self._settings.model_overrides = {
                p: v for p, v in self._settings.model_overrides.items() if v != pid
            }
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_models_page()

    def _on_add_provider(self, button):
        if not isinstance(self._settings.providers, dict):
            self._settings.providers = {}
        base, i, pid = 'provider', 1, 'provider'
        while pid in self._settings.providers:
            i += 1
            pid = f'{base}{i}'
        self._settings.providers[pid] = {
            'name': '', 'base_url': '', 'api_key': '', 'models': [],
        }
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_models_page()

    def _on_load_recommended_preset(self, button):
        """Confirm, then upsert the localhost ollama-pool provider + tier mapping.

        Overwrites the default provider and the four tier assignments; existing
        providers are kept. The confirm dialog guards against surprising a user
        who has hand-tuned their tiers.
        """
        dialog = Adw.AlertDialog.new(
            'Load recommended preset?',
            'Sets the default provider to the localhost ollama pool and '
            'overwrites the four tier assignments. Existing providers are '
            'kept. Continue?')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('load', 'Load')
        dialog.set_response_appearance('load', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def on_response(d, response_id):
            if response_id != 'load':
                return
            apply_recommended_preset(self._settings)
            self._settings.save()
            self._app.emit('settings-changed')
            self._refresh_models_page()

        dialog.connect('response', on_response)
        dialog.present(self)

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
        desc_row.set_subtitle('GTK4 desktop cockpit for AI coding harnesses')
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