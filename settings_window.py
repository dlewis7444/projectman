import os
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk

from settings import TIERS
from models import (build_provider_options, build_tier_options,
                    list_provider_models, normalize_model_id)


class ProviderEditorWindow(Adw.Window):
    """A resizeable sub-window for editing one provider: name, base URL, API
    key, tier assignments, and model list — with save-on-change and an async
    model-reachability indicator.

    Replaces the in-page ``Adw.ExpanderRow`` card and fixes the two bugs that
    card had: (1) no full rebuild mid-edit, so adding a model or saving a field
    never collapses/closes the editor or yanks focus out of the entry being
    typed; (2) Name/Base URL save on apply AND focus-out (not apply-only), so a
    type-and-move-on doesn't lose them. On close it calls back into the owning
    SettingsWindow once to refresh the slim provider-row list.
    """

    def __init__(self, settings, app, parent, pid, on_close=None):
        super().__init__()
        self._settings = settings
        self._app = app
        self._pid = pid
        self._on_close = on_close
        self._suppress = False
        self._model_row_for = {}      # mid -> Adw.ActionRow
        self._add_model_row = None

        prov = settings.providers.get(pid) \
            if isinstance(settings.providers, dict) else None
        if not isinstance(prov, dict):
            prov = {}
        self._prov = prov

        self.set_title(f'{prov.get("name") or pid} Models')
        self.set_default_size(560, 640)
        if parent is not None:
            self.set_transient_for(parent)
        self.set_modal(False)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(620)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        clamp.set_child(outer)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

        # -- Identity: Name / Base URL / API Key --
        ident = Adw.PreferencesGroup(title='Provider')

        self._name_row = Adw.EntryRow(title='Name')
        self._name_row.set_text(prov.get('name') or '')
        self._name_row.set_show_apply_button(True)
        self._name_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._name_row.connect('apply', lambda _r: self._commit_name())
        self._wire_focus_commit(self._name_row, self._commit_name)
        ident.add(self._name_row)

        self._url_row = Adw.EntryRow(title='Base URL')
        self._url_row.set_text(prov.get('base_url') or '')
        self._url_row.set_show_apply_button(True)
        self._url_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._url_row.set_tooltip_text('e.g. http://localhost:11434')
        self._url_row.connect('apply', lambda _r: self._commit_url())
        self._wire_focus_commit(self._url_row, self._commit_url)
        ident.add(self._url_row)

        key_row = Adw.ActionRow(title='API Key')
        key = prov.get('api_key') or ''
        key_row.set_subtitle(f'••••{key[-4:]}' if key else 'Not set')
        self._key_entry = Gtk.PasswordEntry()
        self._key_entry.set_show_peek_icon(True)
        self._key_entry.set_text(key)
        self._key_entry.set_valign(Gtk.Align.CENTER)
        self._key_entry.set_size_request(240, -1)
        self._key_entry.set_tooltip_text('Sent as ANTHROPIC_AUTH_TOKEN')
        self._key_entry.connect('notify::text',
            lambda e, _p, r=key_row: self._on_key_changed(e, r))
        key_row.add_suffix(self._key_entry)
        ident.add(key_row)
        outer.append(ident)

        # -- Tier assignments (ABOVE the models list) --
        self._tier_group = Adw.PreferencesGroup(
            title='Tier assignments',
            description='Opus/Sonnet/Haiku/Subagent → a model on this provider. '
                        '"Default" uses the first model.')
        self._tier_combos = {}
        self._rebuild_tier_combos()
        outer.append(self._tier_group)

        # -- Classifier levers --
        self._classifier_group = Adw.PreferencesGroup(
            title='Classifier',
            description='Auto-mode classifier tuning. Leave unset to use Claude '
                        'Code defaults.')
        self._classifier_combos = {}
        self._classifier_temp_row = None
        self._classifier_two_stage_row = None
        self._rebuild_classifier_group()
        outer.append(self._classifier_group)

        # -- Models --
        self._models_group = Adw.PreferencesGroup(title='Models')
        self._rebuild_models_group()
        outer.append(self._models_group)

        # -- Remove provider --
        rm_group = Adw.PreferencesGroup()
        rm_row = Adw.ActionRow()
        rm_btn = Gtk.Button(label='Remove provider')
        rm_btn.add_css_class('destructive-action')
        rm_btn.set_valign(Gtk.Align.CENTER)
        rm_btn.connect('clicked', self._on_remove_provider)
        rm_row.add_suffix(rm_btn)
        rm_group.add(rm_row)
        outer.append(rm_group)

        self.connect('close-request', self._on_close_request)
        self.present()

    # --- construction helpers -----------------------------------------

    def _wire_focus_commit(self, row, commit):
        """Commit a field when focus leaves its entry (apply/Enter covers the
        explicit-save path; this covers type-and-move-on). Stashes the
        controller on the row so tests can emit 'leave' directly."""
        fc = Gtk.EventControllerFocus()
        fc.connect('leave', lambda _c: commit())
        row.add_controller(fc)
        row._focus_ctrl = fc

    def _rebuild_tier_combos(self):
        """Rebuild the tier ComboRows from the provider's current model list,
        preserving each combo's current selection. Tier combos are discrete
        selections (not being typed), so rebuilding them when the model list
        changes is safe and doesn't disturb any entry's focus."""
        for combo in self._tier_combos.values():
            self._tier_group.remove(combo)
        self._tier_combos = {}

        tier_ids, tier_labels = build_tier_options(
            self._settings.providers, self._pid)
        tier_sub = self._settings.tier_models.get(self._pid, {}) \
            if isinstance(self._settings.tier_models, dict) else {}
        if not isinstance(tier_sub, dict):
            tier_sub = {}

        for tier, label in (('opus', 'Opus'), ('sonnet', 'Sonnet'),
                            ('haiku', 'Haiku'), ('subagent', 'Subagent'),
                            ('fable', 'Fable (future?)')):
            combo = Adw.ComboRow(title=label)
            combo.set_model(Gtk.StringList.new(tier_labels))
            val = tier_sub.get(tier, '')
            if not isinstance(val, str) or val not in tier_ids:
                val = ''
            self._suppress = True
            try:
                combo.set_selected(tier_ids.index(val) if val in tier_ids else 0)
            finally:
                self._suppress = False
            combo.connect('notify::selected',
                          lambda r, _p, t=tier: self._on_tier_changed(t, r))
            if tier == 'fable':
                # Forward-looking placeholder: CC has a Fable model but no
                # documented per-tier default env var yet. Wired to the env
                # like the others (build_spawn_env emits
                # ANTHROPIC_DEFAULT_FABLE_MODEL) but not user-adjustable until
                # CC honors it.
                combo.set_sensitive(False)
            self._tier_group.add(combo)
            self._tier_combos[tier] = combo

    def _rebuild_classifier_group(self):
        """Rebuild the classifier ComboRows + temperature/two-stage widgets,
        preserving current selections/values. Like tier combos, these are
        discrete selections so rebuilding when the model list changes is safe."""
        for combo in self._classifier_combos.values():
            self._classifier_group.remove(combo)
        self._classifier_combos = {}
        if self._classifier_temp_row is not None:
            self._classifier_group.remove(self._classifier_temp_row)
            self._classifier_temp_row = None
        if self._classifier_two_stage_row is not None:
            self._classifier_group.remove(self._classifier_two_stage_row)
            self._classifier_two_stage_row = None

        ids, labels = build_tier_options(
            self._settings.providers, self._pid)
        cm = self._settings.classifier_models.get(self._pid, {}) \
            if isinstance(self._settings.classifier_models, dict) else {}
        if not isinstance(cm, dict):
            cm = {}

        for kind, title in (('auto_mode', 'Auto-mode model'),
                            ('bg_classifier', 'Background classifier')):
            combo = Adw.ComboRow(title=title)
            combo.set_model(Gtk.StringList.new(labels))
            val = cm.get(kind, '')
            if not isinstance(val, str) or val not in ids:
                val = ''
            self._suppress = True
            try:
                combo.set_selected(ids.index(val) if val in ids else 0)
            finally:
                self._suppress = False
            combo.connect('notify::selected',
                          lambda r, _p, k=kind: self._on_classifier_model_changed(k, r))
            self._classifier_group.add(combo)
            self._classifier_combos[kind] = combo

        temp_val = ''
        ct = self._settings.classifier_temperature.get(self._pid) \
            if isinstance(self._settings.classifier_temperature, dict) else None
        if isinstance(ct, (int, float)):
            temp_val = str(float(ct))
        temp_row = Adw.EntryRow(title='Classifier temperature')
        temp_row.set_text(temp_val)
        temp_row.set_show_apply_button(True)
        temp_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        temp_row.set_tooltip_text('Temperature passed to CLAUDE_CODE_AUTO_MODE_TEMPERATURE')
        temp_row.connect('apply', lambda _r: self._commit_classifier_temperature())
        self._wire_focus_commit(temp_row, self._commit_classifier_temperature)
        self._classifier_group.add(temp_row)
        self._classifier_temp_row = temp_row

        two_stage = False
        c2 = self._settings.classifier_two_stage.get(self._pid) \
            if isinstance(self._settings.classifier_two_stage, dict) else None
        if isinstance(c2, bool):
            two_stage = c2
        ts_row = Adw.SwitchRow(title='Two-stage classifier')
        ts_row.set_active(two_stage)
        ts_row.set_tooltip_text('Sets CLAUDE_CODE_TWO_STAGE_CLASSIFIER to 1 or 0')
        ts_row.connect('notify::active', self._on_classifier_two_stage_changed)
        self._classifier_group.add(ts_row)
        self._classifier_two_stage_row = ts_row

    def _rebuild_models_group(self):
        """Rebuild the model ActionRows + the Add-model entry. The Name/Base
        URL/Key entries live in a separate group, so this never destroys an
        entry the user is mid-typing in (Add-model apply already fired before
        this is called from _on_add_model)."""
        for row in list(self._model_row_for.values()):
            self._models_group.remove(row)
        self._model_row_for = {}
        if self._add_model_row is not None:
            self._models_group.remove(self._add_model_row)
            self._add_model_row = None

        models = self._prov.get('models') \
            if isinstance(self._prov.get('models'), list) else []
        for mid in models:
            if isinstance(mid, str):
                self._models_group.add(self._build_model_row(mid))

        add_row = Adw.EntryRow(title='Add model')
        add_row.set_show_apply_button(True)
        add_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        add_row.set_tooltip_text('Free-text id, e.g. glm-5.2:cloud[1m]')
        add_row.connect('apply', self._on_add_model)
        self._models_group.add(add_row)
        self._add_model_row = add_row

    def _build_model_row(self, mid):
        row = Adw.ActionRow(title=mid)
        row.set_tooltip_text(mid)
        rm = Gtk.Button.new_from_icon_name('list-remove-symbolic')
        rm.add_css_class('flat')
        rm.set_valign(Gtk.Align.CENTER)
        rm.set_tooltip_text('Remove model')
        rm.connect('clicked', lambda _b, m=mid: self._on_remove_model(m))
        row.add_suffix(rm)
        self._model_row_for[mid] = row
        return row

    # --- save + notify ------------------------------------------------

    def _save_and_notify(self):
        self._settings.save()
        self._app.emit('settings-changed')

    def _commit_name(self):
        name = self._name_row.get_text().strip()
        if self._prov.get('name') == name:
            return
        self._prov['name'] = name
        self.set_title(f'{name or self._pid} Models')
        self._save_and_notify()

    def _commit_url(self):
        url = self._url_row.get_text().strip()
        if self._prov.get('base_url') == url:
            return
        self._prov['base_url'] = url
        self._save_and_notify()

    def _on_key_changed(self, entry, key_row):
        key = entry.get_text()
        self._prov['api_key'] = key
        key_row.set_subtitle(f'••••{key[-4:]}' if key else 'Not set')
        self._save_and_notify()

    def _on_tier_changed(self, tier, row):
        if self._suppress:
            return
        ids, _labels = build_tier_options(self._settings.providers, self._pid)
        idx = row.get_selected()
        if not isinstance(self._settings.tier_models, dict):
            self._settings.tier_models = {}
        sub = self._settings.tier_models.setdefault(self._pid, {})
        if not isinstance(sub, dict):
            sub = {}
            self._settings.tier_models[self._pid] = sub
        if 0 <= idx < len(ids):
            sub[tier] = ids[idx]
            self._save_and_notify()

    def _on_classifier_model_changed(self, kind, row):
        if self._suppress:
            return
        ids, _labels = build_tier_options(self._settings.providers, self._pid)
        idx = row.get_selected()
        if not isinstance(self._settings.classifier_models, dict):
            self._settings.classifier_models = {}
        sub = self._settings.classifier_models.setdefault(self._pid, {})
        if not isinstance(sub, dict):
            sub = {}
            self._settings.classifier_models[self._pid] = sub
        if 0 <= idx < len(ids):
            sub[kind] = ids[idx]
            self._save_and_notify()

    def _commit_classifier_temperature(self):
        text = self._classifier_temp_row.get_text().strip()
        if not text:
            if (isinstance(self._settings.classifier_temperature, dict)
                    and self._pid in self._settings.classifier_temperature):
                self._settings.classifier_temperature.pop(self._pid, None)
                self._save_and_notify()
            return
        try:
            value = float(text)
        except ValueError:
            return
        if not __import__('math').isfinite(value):
            return
        if not isinstance(self._settings.classifier_temperature, dict):
            self._settings.classifier_temperature = {}
        self._settings.classifier_temperature[self._pid] = value
        self._save_and_notify()

    def _on_classifier_two_stage_changed(self, row, _param):
        if self._suppress:
            return
        if not isinstance(self._settings.classifier_two_stage, dict):
            self._settings.classifier_two_stage = {}
        self._settings.classifier_two_stage[self._pid] = bool(row.get_active())
        self._save_and_notify()

    def _on_add_model(self, row):
        mid = row.get_text().strip()
        if not mid:
            return
        raw = self._prov.get('models')
        models = raw if isinstance(raw, list) else []
        if mid not in models:
            self._prov['models'] = list(models) + [mid]
            self._save_and_notify()
            self._rebuild_tier_combos()   # new model is now a tier option
            self._rebuild_classifier_group()  # classifier picks see it too
        # Rebuild refreshes the model rows + a fresh Add entry. apply already
        # fired, so no entry the user is editing is destroyed mid-typing.
        self._rebuild_models_group()
        if mid in (self._prov.get('models') or []):
            self._probe_model(mid)

    def _on_remove_model(self, mid):
        self._prov['models'] = [m for m in self._prov.get('models', []) if m != mid]
        # Drop tier pins that referenced the removed model.
        if isinstance(self._settings.tier_models, dict):
            sub = self._settings.tier_models.get(self._pid)
            if isinstance(sub, dict):
                for tier in TIERS:
                    if sub.get(tier) == mid:
                        sub[tier] = ''
        # Drop classifier model picks that referenced the removed model.
        if isinstance(self._settings.classifier_models, dict):
            sub = self._settings.classifier_models.get(self._pid)
            if isinstance(sub, dict):
                for kind in ('auto_mode', 'bg_classifier'):
                    if sub.get(kind) == mid:
                        sub[kind] = ''
        self._save_and_notify()
        self._rebuild_tier_combos()
        self._rebuild_classifier_group()
        self._rebuild_models_group()

    def _on_remove_provider(self, _btn):
        if isinstance(self._settings.providers, dict):
            self._settings.providers.pop(self._pid, None)
        if self._settings.model_default == self._pid:
            self._settings.model_default = ''
        if isinstance(self._settings.model_overrides, dict):
            self._settings.model_overrides = {
                p: v for p, v in self._settings.model_overrides.items()
                if v != self._pid
            }
        if isinstance(self._settings.tier_models, dict):
            self._settings.tier_models.pop(self._pid, None)
        if isinstance(self._settings.classifier_models, dict):
            self._settings.classifier_models.pop(self._pid, None)
        if isinstance(self._settings.classifier_temperature, dict):
            self._settings.classifier_temperature.pop(self._pid, None)
        if isinstance(self._settings.classifier_two_stage, dict):
            self._settings.classifier_two_stage.pop(self._pid, None)
        self._save_and_notify()
        self._teardown()
        self.destroy()

    # --- async reachability probe -------------------------------------

    def _probe_model(self, mid):
        """Advisory ping: is ``mid`` offered by the provider's endpoint? Runs
        off the main loop; sets a green/amber indicator on the model row when
        it returns. The model is always kept regardless of the result."""
        prov_snap = dict(self._prov)

        def work():
            offered = list_provider_models(prov_snap)
            reachable = offered is not None
            found = bool(offered) and (normalize_model_id(mid) in offered)
            GLib.idle_add(self._apply_probe_result, mid, found, reachable)

        threading.Thread(target=work, daemon=True).start()

    def _apply_probe_result(self, mid, found, reachable):
        row = self._model_row_for.get(mid)
        if row is None or row.get_parent() is None:
            return False
        if found:
            icon_name, tip, cls = ('emblem-ok-symbolic',
                                   'found on provider', 'success')
        elif reachable:
            icon_name, tip, cls = ('dialog-warning-symbolic',
                                   'not found on provider — kept anyway', 'warning')
        else:
            icon_name, tip, cls = ('dialog-warning-symbolic',
                                   'provider unreachable — kept anyway', 'warning')
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_tooltip_text(tip)
        icon.set_valign(Gtk.Align.CENTER)
        if cls:
            icon.add_css_class(cls)
        # Replace any prior probe indicator on this row.
        prior = getattr(row, '_probe_icon', None)
        if prior is not None:
            row.remove_suffix(prior)
        row.add_suffix(icon)
        row._probe_icon = icon
        return False

    # --- lifecycle ----------------------------------------------------

    def _teardown(self):
        """Commit any pending field edits as a focus-out safety net, then
        refresh the owning SettingsWindow's slim provider-row list once."""
        self._commit_name()
        self._commit_url()
        if self._on_close is not None:
            self._on_close()

    def _on_close_request(self, _w):
        self._teardown()

    def _on_key_pressed(self, controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False


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
                        '(ollama, LiteLLM, etc.). Pick a default provider and '
                        'define providers below — each provider card carries its '
                        'own tier assignments. Override the provider per project '
                        'from the sidebar menu. Under Zellij the provider applies '
                        'to new sessions only (an attach inherits the server env).',
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

        # -- Providers --
        # Each provider card holds its own per-provider Tier Assignments (B2):
        # TA applies to any defined provider, not just the default, so the combos
        # live in the card instead of a separate group gated on the default.
        self._providers_group = Adw.PreferencesGroup(
            title='Providers',
            description='Define Anthropic-compatible providers and their models. '
                        'Each card carries its own tier assignments.',
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
            row = self._build_provider_row(pid, prov)
            self._providers_group.add(row)
            self._provider_card_rows.append(row)

    def _build_provider_row(self, pid, prov):
        """A slim, one-line row for a provider in the Models page. The row's
        title is the provider's display name (or pid) — this preserves the gate
        walk's ``has('Ollama')`` assertion — and the "Models" button opens the
        full editor sub-window. No tier combos or entries live here, so this
        row is never the thing being typed into."""
        name = prov.get('name') or pid
        row = Adw.ActionRow(title=name, subtitle=pid)
        models = prov.get('models') if isinstance(prov.get('models'), list) else []
        n = len([m for m in models if isinstance(m, str)])
        if n:
            count = Gtk.Label(label=f'{n} model{"s" if n != 1 else ""}')
            count.add_css_class('dim-label')
            count.set_valign(Gtk.Align.CENTER)
            row.add_suffix(count)
        btn = Gtk.Button(label='Models')
        btn.add_css_class('suggested-action')
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect('clicked', lambda _b, p=pid: self._open_editor(p))
        row.add_suffix(btn)
        return row

    def _open_editor(self, pid):
        """Open the provider editor sub-window. On close it refreshes this page
        once (the slim row's name/model-count may have changed)."""
        ProviderEditorWindow(self._settings, self._app, self, pid,
                             on_close=self._refresh_models_page)

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
        # Tier assignments are per-provider (in each provider's editor), so
        # changing the default doesn't invalidate any provider's tiers — just
        # refresh the combo + slim-row layout.
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
        # Open the editor on the freshly-added empty provider so the user can
        # fill it immediately (the flow that lost fields under the ExpanderRow).
        self._open_editor(pid)

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