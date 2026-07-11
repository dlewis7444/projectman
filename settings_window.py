import os
import threading
from urllib.parse import urlparse

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from settings import TIERS
from models import (build_provider_options, build_tier_options,
                    list_provider_models, normalize_model_id,
                    is_1m_model_id, with_1m_suffix, without_1m_suffix)


class ProviderEditorWindow(Adw.Dialog):
    """A resizeable sub-window for editing one provider: name, base URL, API
    key, tier assignments, and model list — with save-on-close (one disk write
    per editing session) and an async model-reachability indicator.

    Edits mutate the shared live ``Settings`` object immediately, so the owning
    window sees changes as they're made; the ``settings.json`` disk write is
    deferred to ``closed`` (``_teardown``) — one write per session instead of
    one per keystroke. Replaces the in-page ``Adw.ExpanderRow`` card and fixes
    the two bugs that card had: (1) no full rebuild mid-edit, so adding a model
    or saving a field never collapses/closes the editor or yanks focus out of
    the entry being typed; (2) Name/Base URL commit on apply AND focus-out (not
    apply-only), so a type-and-move-on doesn't lose them. On close it calls
    back into the owning SettingsWindow once to refresh the slim provider-row
    list.
    """

    def __init__(self, settings, app, parent, pid, on_close=None):
        super().__init__()
        self._settings = settings
        self._app = app
        self._pid = pid
        self._on_close = on_close
        self._suppress = False
        self._closed = False           # set in _teardown; guards async probe callbacks
        self._dirty = False           # in-memory Settings mutated; flushed to disk once on close
        self._model_row_for = {}      # mid -> Adw.ActionRow
        self._add_model_row = None

        prov = settings.providers.get(pid) \
            if isinstance(settings.providers, dict) else None
        if not isinstance(prov, dict):
            prov = {}
        self._prov = prov

        self.set_title(f'{prov.get("name") or pid} Models')
        # Adw.Dialog (was Adw.Window): a dialog layers above the Settings
        # PreferencesDialog properly — the Adw.Window version opened BEHIND it
        # on a real desktop ("Models button does nothing"), and
        # set_transient_for on the PreferencesDialog parent raised TypeError
        # (PreferencesDialog is not a Gtk.Window). Adw.Dialog.present() takes any
        # widget as parent (not just Gtk.Window), dialogs are always modal (so
        # this stays above Settings — fixes the layering), and Escape/close are
        # handled by the dialog itself. See libadwaita
        # migrating-to-adaptive-dialogs.
        self.set_content_width(560)
        self.set_content_height(640)

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
        self.set_child(toolbar_view)

        # -- Identity: Name / Base URL / API Key / Max context tokens --
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

        # Max context tokens — just below API Key (not a separate top group).
        _MAX_CTX_TIP = (
            'Set the max tokens for non-1M models. If blank, harness will use '
            'its default (200k for Claude Code).')
        self._max_ctx_row = Adw.EntryRow(title='Max context tokens')
        mct = prov.get('max_context_tokens')
        if isinstance(mct, int) and mct > 0:
            self._max_ctx_row.set_text(str(mct))
        else:
            self._max_ctx_row.set_text('')
        self._max_ctx_row.set_show_apply_button(True)
        self._max_ctx_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self._max_ctx_row.set_tooltip_text(_MAX_CTX_TIP)
        self._max_ctx_row.connect(
            'apply', lambda _r: self._commit_max_context_tokens())
        self._wire_focus_commit(
            self._max_ctx_row, self._commit_max_context_tokens)
        ident.add(self._max_ctx_row)
        outer.append(ident)

        # -- Models (ABOVE tier assignments — the maintainer wants the models list first,
        #    since picking models precedes assigning them to tiers) --
        self._models_group = Adw.PreferencesGroup(
            title='Models',
            description='Use the 1M control on each row to mark long-context '
                        'models (Claude Code [1m] suffix).',
        )
        self._rebuild_models_group()
        outer.append(self._models_group)

        # -- Server-fed model picker --
        self._picker_group = Adw.PreferencesGroup(
            title='Server models',
            description='Pick from the models advertised by the provider. '
                        'Manually added models are kept even when the server is offline.')
        self._picker_expander = Adw.ExpanderRow(title='Select Models…')
        self._picker_expander.connect('notify::expanded', self._on_picker_expanded)
        self._picker_group.add(self._picker_expander)
        self._picker_rows = {}
        self._picker_loading_row = None
        outer.append(self._picker_group)

        # -- Tier assignments --
        self._tier_group = Adw.PreferencesGroup(
            title='Tier assignments',
            description='Opus/Sonnet/Haiku/Subagent/Fable → a model on this '
                        'provider. "Default" uses the first model.')
        self._tier_combos = {}
        self._rebuild_tier_combos()
        outer.append(self._tier_group)

        # -- Classifier temperature (the only live lever) --
        self._classifier_group = Adw.PreferencesGroup(
            title='Classifier',
            description='Auto-mode classifier temperature. Leave unset to use '
                        "Claude Code's default.")
        self._classifier_temp_row = None
        self._rebuild_classifier_group()
        outer.append(self._classifier_group)

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

        # Adw.Dialog commits/refreshes on 'closed' (fires AFTER the dialog has
        # dismissed, on Escape / back / close()). We use 'closed' rather than
        # 'close-attempt': with the default can-close=True, Adw.Dialog emits
        # 'closed' on a normal close and only emits 'close-attempt' when
        # can-close=False — so 'close-attempt' never fired on a real desktop and
        # the owner's Models page never refreshed. The GObject + its entries
        # outlive the dismiss, so _teardown can still commit pending edits.
        self.connect('closed', lambda *_a: self._teardown())
        self.present(parent)

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
                            ('fable', 'Fable')):
            combo = Adw.ComboRow(title=label)
            combo.set_model(Gtk.StringList.new(tier_labels))
            val = tier_sub.get(tier, '')
            if not isinstance(val, str) or val not in tier_ids:
                # Stale saved tier (its model was removed from the provider):
                # align the stored value with the UI's 'Default' selection so
                # the on-disk file doesn't keep a model id that's no longer
                # selectable. tier_sub is the real stored dict when the pid
                # entry exists; a fresh {} (pid absent) has val == '' already.
                if val:
                    tier_sub[tier] = ''
                val = ''
            self._suppress = True
            try:
                combo.set_selected(tier_ids.index(val) if val in tier_ids else 0)
            finally:
                self._suppress = False
            combo.connect('notify::selected',
                          lambda r, _p, t=tier: self._on_tier_changed(t, r))
            self._tier_group.add(combo)
            self._tier_combos[tier] = combo

    def _rebuild_classifier_group(self):
        """Rebuild the classifier temperature entry, preserving the current
        value. The only live classifier lever is
        ``CLAUDE_CODE_AUTO_MODE_TEMPERATURE``; the other env vars are inert in
        Claude Code v2.1.190+ and are no longer exposed."""
        if self._classifier_temp_row is not None:
            self._classifier_group.remove(self._classifier_temp_row)
            self._classifier_temp_row = None

        temp_val = ''
        ct = self._settings.classifier_temperature.get(self._pid) \
            if isinstance(self._settings.classifier_temperature, dict) else None
        if isinstance(ct, (int, float)):
            temp_val = str(float(ct))
        temp_row = Adw.EntryRow(title='Classifier temperature')
        temp_row.set_text(temp_val)
        temp_row.set_show_apply_button(True)
        temp_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        temp_row.set_tooltip_text('Passed to CLAUDE_CODE_AUTO_MODE_TEMPERATURE')
        temp_row.connect('apply', lambda _r: self._commit_classifier_temperature())
        self._wire_focus_commit(temp_row, self._commit_classifier_temperature)
        self._classifier_group.add(temp_row)
        self._classifier_temp_row = temp_row

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
        add_row.set_tooltip_text(
            'Free-text model id (use the 1M toggle on the row for long context)')
        add_row.connect('apply', self._on_add_model)
        self._models_group.add(add_row)
        self._add_model_row = add_row

    def _build_model_row(self, mid):
        bare = normalize_model_id(mid) or mid
        row = Adw.ActionRow(title=bare)
        row.set_tooltip_text(mid)
        # 1M toggle — left of remove; encodes/strips trailing [1m] on the id.
        one_m = Gtk.CheckButton(label='1M')
        one_m.set_valign(Gtk.Align.CENTER)
        one_m.set_tooltip_text(
            "Ask Claude Code to treat this model as having a 1M-token context "
            "window. When off, Claude Code uses its normal default size for "
            "the provider/model.")
        self._suppress = True
        try:
            one_m.set_active(is_1m_model_id(mid))
        finally:
            self._suppress = False
        one_m.connect(
            'toggled',
            lambda btn, m=mid: self._on_model_1m_toggled(m, btn))
        row.add_suffix(one_m)
        rm = Gtk.Button.new_from_icon_name('list-remove-symbolic')
        rm.add_css_class('flat')
        rm.set_valign(Gtk.Align.CENTER)
        rm.set_tooltip_text('Remove model')
        rm.connect('clicked', lambda _b, m=mid: self._on_remove_model(m))
        row.add_suffix(rm)
        self._model_row_for[mid] = row
        return row

    # --- server-fed model picker --------------------------------------

    def _on_picker_expanded(self, expander, _param):
        """Refresh the picker each time it is opened."""
        if not expander.get_expanded():
            return
        self._fetch_server_models()

    def _fetch_server_models(self):
        """Populate the picker asynchronously so the UI never blocks on the
        network probe. Re-fetch every time the picker opens."""
        self._clear_picker_rows()
        loading = Adw.ActionRow(title='Loading models…')
        self._picker_expander.add_row(loading)
        self._picker_loading_row = loading

        prov_snap = dict(self._prov)

        def work():
            offered = list_provider_models(prov_snap)
            GLib.idle_add(self._populate_picker, offered)

        threading.Thread(target=work, daemon=True).start()

    def _clear_picker_rows(self):
        for row in list(self._picker_rows.values()):
            self._picker_expander.remove(row)
        self._picker_rows = {}
        if self._picker_loading_row is not None:
            self._picker_expander.remove(self._picker_loading_row)
            self._picker_loading_row = None

    def _populate_picker(self, offered):
        """Build the checkbox list from the server result merged with the user's
        current model list. Runs on the main thread via GLib.idle_add."""
        if self._closed:
            return False
        self._clear_picker_rows()

        raw_models = self._prov.get('models') \
            if isinstance(self._prov.get('models'), list) else []
        current = {}
        for m in raw_models:
            if isinstance(m, str):
                current.setdefault(normalize_model_id(m), []).append(m)

        server_ids = offered if offered is not None else set()
        union = {}
        for mid in server_ids:
            nid = normalize_model_id(mid)
            union[nid] = (mid, False)
        for raw in raw_models:
            nid = normalize_model_id(raw)
            if nid not in union:
                union[nid] = (raw, True)

        if offered is None:
            info = Adw.ActionRow(title='Provider unreachable')
            info.set_subtitle('Use Add model below while offline')
            self._picker_expander.add_row(info)

        for nid in sorted(union):
            display, manual = union[nid]
            row = Adw.SwitchRow(title=display)
            if manual:
                row.set_subtitle('manually added')
            active = nid in current
            self._suppress = True
            try:
                row.set_active(active)
            finally:
                self._suppress = False
            row.connect('notify::active',
                        lambda r, _p, n=nid: self._on_picker_model_toggled(n, r))
            self._picker_expander.add_row(row)
            self._picker_rows[nid] = row
        return False

    def _on_picker_model_toggled(self, nid, row):
        """Add or remove a model from the provider when its picker checkbox
        toggles, then rebuild tier/classifier combos so the new selection is
        immediately selectable elsewhere in the editor."""
        if self._suppress:
            return
        raw_models = self._prov.get('models') \
            if isinstance(self._prov.get('models'), list) else []
        active = row.get_active()
        new_models = [m for m in raw_models if normalize_model_id(m) != nid]
        if active:
            new_models.append(row.get_title())
        if new_models != list(raw_models):
            self._prov['models'] = new_models
            self._mark_dirty()
            self._rebuild_tier_combos()
            self._rebuild_classifier_group()
            self._rebuild_models_group()

    # --- save + notify ------------------------------------------------

    def _save_and_notify(self):
        self._settings.save()
        self._app.emit('settings-changed')

    def _mark_dirty(self):
        """Note that the shared live Settings was mutated; the settings.json
        disk write is deferred to dialog close (_teardown). Editor edits
        mutate the in-memory Settings object immediately, so the owning
        window sees changes as they're made — only the persistence is
        batched to one write per editing session (was: one write per
        keystroke in the API-key field)."""
        # TODO: surface an "unsaved changes" affordance (Apply sensitivity / a
        # dirty dot) — edits persist only on dialog close, with no visible signal
        # that Apply/Enter didn't already write to disk. (release gate mission-
        # provider, )
        self._dirty = True

    def _commit_name(self):
        name = self._name_row.get_text().strip()
        if self._prov.get('name') == name:
            return
        self._prov['name'] = name
        self.set_title(f'{name or self._pid} Models')
        self._mark_dirty()

    def _commit_max_context_tokens(self):
        tip = (
            'Set the max tokens for non-1M models. If blank, harness will use '
            'its default (200k for Claude Code).')
        err = 'Enter a positive whole number, or leave blank'
        text = self._max_ctx_row.get_text().strip()
        if not text:
            if 'max_context_tokens' in self._prov:
                self._prov.pop('max_context_tokens', None)
                self._mark_dirty()
            self._max_ctx_row.remove_css_class('error')
            self._max_ctx_row.set_tooltip_text(tip)
            return
        if not text.isdigit():
            self._max_ctx_row.add_css_class('error')
            self._max_ctx_row.set_tooltip_text(err)
            return
        value = int(text)
        if value <= 0:
            self._max_ctx_row.add_css_class('error')
            self._max_ctx_row.set_tooltip_text(err)
            return
        self._max_ctx_row.remove_css_class('error')
        self._max_ctx_row.set_tooltip_text(tip)
        if self._prov.get('max_context_tokens') == value:
            return
        self._prov['max_context_tokens'] = value
        self._mark_dirty()

    def _commit_url(self):
        url = self._url_row.get_text().strip()
        if self._prov.get('base_url') == url:
            # Same as stored value: if it's valid, no-op; if it's somehow
            # invalid it cannot have gotten into _prov through this path.
            self._clear_url_error()
            return
        if url == '':
            # Empty means "no base URL" — models.py skips the provider
            # gracefully, so treat it as valid (clears the field).
            self._prov['base_url'] = ''
            self._clear_url_error()
            self._mark_dirty()
            return
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            self._show_url_error(
                'Base URL must be http:// or https:// with a valid host')
            return
        self._prov['base_url'] = url
        self._clear_url_error()
        self._mark_dirty()

    def _show_url_error(self, message):
        self._url_row.add_css_class('error')
        self._url_row.set_tooltip_text(message)

    def _clear_url_error(self):
        self._url_row.remove_css_class('error')
        self._url_row.set_tooltip_text('e.g. http://localhost:11434')

    def _on_key_changed(self, entry, key_row):
        key = entry.get_text()
        self._prov['api_key'] = key
        key_row.set_subtitle(f'••••{key[-4:]}' if key else 'Not set')
        self._mark_dirty()

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
            self._mark_dirty()

    def _commit_classifier_temperature(self):
        text = self._classifier_temp_row.get_text().strip()
        if not text:
            if (isinstance(self._settings.classifier_temperature, dict)
                    and self._pid in self._settings.classifier_temperature):
                self._settings.classifier_temperature.pop(self._pid, None)
                self._mark_dirty()
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
        self._mark_dirty()

    def _on_add_model(self, row):
        mid = row.get_text().strip()
        if not mid:
            return
        raw = self._prov.get('models')
        models = raw if isinstance(raw, list) else []
        if mid not in models:
            self._prov['models'] = list(models) + [mid]
            self._mark_dirty()
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
        self._mark_dirty()
        self._rebuild_tier_combos()
        self._rebuild_classifier_group()
        self._rebuild_models_group()

    def _on_model_1m_toggled(self, mid, btn):
        """Rewrite the stored model id to add/strip trailing [1m], and retarget
        any tier pins that still name the old id."""
        if self._suppress:
            return
        want_1m = btn.get_active()
        new_mid = with_1m_suffix(mid) if want_1m else without_1m_suffix(mid)
        if new_mid == mid:
            return
        raw = self._prov.get('models')
        models = list(raw) if isinstance(raw, list) else []
        try:
            idx = models.index(mid)
        except ValueError:
            return
        # Avoid duplicates if both bare and [1m] forms already exist.
        if new_mid in models:
            models.pop(idx)
        else:
            models[idx] = new_mid
        self._prov['models'] = models
        if isinstance(self._settings.tier_models, dict):
            sub = self._settings.tier_models.get(self._pid)
            if isinstance(sub, dict):
                for tier in TIERS:
                    if sub.get(tier) == mid:
                        sub[tier] = new_mid if new_mid in models else ''
        self._mark_dirty()
        self._rebuild_tier_combos()
        self._rebuild_classifier_group()
        self._rebuild_models_group()

    def _on_remove_provider(self, _btn):
        if isinstance(self._settings.providers, dict):
            self._settings.providers.pop(self._pid, None)
        if self._settings.model_default == self._pid:
            self._settings.model_default = ''
        if isinstance(self._settings.provider_overrides, dict):
            self._settings.provider_overrides = {
                p: v for p, v in self._settings.provider_overrides.items()
                if v != self._pid
            }
        if isinstance(self._settings.tier_models, dict):
            self._settings.tier_models.pop(self._pid, None)
        if isinstance(self._settings.classifier_temperature, dict):
            self._settings.classifier_temperature.pop(self._pid, None)
        self._mark_dirty()
        # close() dismisses the dialog → 'closed' fires → _teardown persists
        # the (now dirty) removal and refreshes the owner's Models page.
        # (Replaces the old _teardown() + destroy() pair.)
        self.close()

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
        # The probe runs on a daemon thread; the editor may have been closed
        # (and its widgets disposed) between firing GLib.idle_add and this
        # callback landing. Bail before touching any widget to avoid a
        # use-after-free / disposed-member access.
        if self._closed:
            return False
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
        """Commit any pending field edits to the in-memory Settings, then
        persist once on close. The editor mutates the shared live Settings
        object on each edit but defers the settings.json disk write to here —
        one write per editing session (was: one per keystroke in the API-key
        field). Finally refresh the owning SettingsWindow's slim provider-row
        list once."""
        self._closed = True
        self._commit_name()
        self._commit_url()
        self._commit_max_context_tokens()
        self._commit_classifier_temperature()
        if self._dirty:
            self._save_and_notify()
        if self._on_close is not None:
            self._on_close()

    # Adw.Dialog has no close-request signal and handles Escape itself; the
    # 'closed' → _teardown wiring is in __init__. (The old _on_close_request
    # + _on_key_pressed handlers were removed in the Adw.Window → Adw.Dialog
    # refactor — see libadwaita migrating-to-adaptive-dialogs.)


class SettingsWindow(Adw.PreferencesDialog):
    def __init__(self, settings, app, parent):
        super().__init__()
        self._settings = settings
        self._app = app
        # `parent` is the owning AppWindow (a Gtk.Window). Store it: we are an
        # Adw.PreferencesDialog (an Adw.Dialog, NOT a Gtk.Window), and
        # Gtk.FileDialog.select_folder() requires a Gtk.Window parent — passing
        # `self` raises TypeError and the folder picker silently does nothing.
        self._parent = parent
        self.set_title('Settings')
        # Guards so programmatic set_selected() during a refresh doesn't
        # re-enter the change handlers and recurse.
        self._suppress_combos = False
        self._build_general_page()
        self._build_hosts_page()
        self._build_terminal_page()
        self._build_paa_page()
        self._build_appearance_page()
        self._build_models_page()
        self._build_harnesses_page()
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
        # page — Claude binary also lives on the Harnesses page; General keeps a
        # convenience row). resolved_claude_binary reads agents['claude']['binary']
        # first, falling back to the legacy claude_binary key.
        claude_group = Adw.PreferencesGroup(title='Claude Code')
        page.add(claude_group)

        cfg = (self._settings.harnesses.get('claude')
               if isinstance(self._settings.harnesses, dict) else None)
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

    def _build_hosts_page(self):
        """Remote SSH hosts (project SoT on the remote)."""
        page = Adw.PreferencesPage(
            title='Hosts', icon_name='network-server-symbolic'
        )
        self.add(page)

        health_group = Adw.PreferencesGroup(
            title='Health checks',
            description='Interval for remote reachability checks. 0 disables.',
        )
        page.add(health_group)
        self._health_interval_row = Adw.SpinRow(
            title='Remote health interval (seconds)',
            subtitle='Default 30. Grey health dots when set to 0.',
            adjustment=Gtk.Adjustment(
                value=self._settings.remote_health_interval_sec,
                lower=0, upper=3600, step_increment=5, page_increment=30,
            ),
            digits=0,
        )
        self._health_interval_row.connect(
            'notify::value', self._on_health_interval_changed)
        health_group.add(self._health_interval_row)

        self._hosts_page = page
        self._add_group = Adw.PreferencesGroup(
            title='Add host',
            description=(
                'Projects live under ~/.ProjectMan/projects on the remote. '
                'Uses your existing SSH config and keys.'
            ),
        )
        page.add(self._add_group)

        self._add_host_btn = Gtk.Button(label='Add Host…')
        self._add_host_btn.add_css_class('pill')
        self._add_host_btn.add_css_class('suggested-action')
        self._add_host_btn.set_halign(Gtk.Align.START)
        self._add_host_btn.connect('clicked', self._on_add_host_clicked)
        add_row = Adw.ActionRow(
            title='Add a remote',
            subtitle='SSH target from your config (e.g. agentbox)',
        )
        add_row.add_suffix(self._add_host_btn)
        add_row.set_activatable_widget(self._add_host_btn)
        self._add_group.add(add_row)

        # Visible busy status (toasts alone are easy to miss in Preferences).
        self._add_host_status = Gtk.Label(label='')
        self._add_host_status.add_css_class('dim-label')
        self._add_host_status.add_css_class('caption')
        self._add_host_status.set_halign(Gtk.Align.START)
        self._add_host_status.set_wrap(True)
        self._add_host_status.set_margin_start(12)
        self._add_host_status.set_margin_end(12)
        self._add_host_status.set_margin_bottom(8)
        self._add_host_status.set_visible(False)
        status_row = Adw.ActionRow()
        status_row.set_child(self._add_host_status)
        status_row.set_activatable(False)
        status_row.set_selectable(False)
        self._add_group.add(status_row)
        self._add_host_status_row = status_row

        self._host_rows = {}
        self._host_switches = {}
        # One PreferencesGroup ("card") per host — torn down on refresh.
        self._host_card_groups = []
        self._refresh_host_rows()

    def _refresh_host_rows(self):
        for group in self._host_card_groups:
            self._hosts_page.remove(group)
        self._host_card_groups.clear()
        self._host_rows.clear()
        self._host_switches.clear()
        for hid, prof in self._settings.host_profiles().items():
            # Separate card so Edit / rich-status clearly belong to this host.
            card = Adw.PreferencesGroup(
                title=prof.title(),
                description=f'SSH: {prof.ssh_target}',
            )
            self._hosts_page.add(card)
            self._host_card_groups.append(card)

            row = Adw.ActionRow(
                title='Connection',
                subtitle=prof.ssh_target,
            )
            edit_btn = Gtk.Button(label='Edit')
            edit_btn.add_css_class('flat')
            edit_btn.connect(
                'clicked',
                lambda b, h=hid: self._open_host_editor(h),
            )
            row.add_suffix(edit_btn)
            remove_btn = Gtk.Button(label='Remove')
            remove_btn.add_css_class('flat')
            remove_btn.add_css_class('destructive-action')
            remove_btn.connect(
                'clicked',
                lambda b, h=hid: self._on_remove_host(h),
            )
            row.add_suffix(remove_btn)
            card.add(row)
            self._host_rows[hid] = row

            sw_row = Adw.SwitchRow(
                title='Rich status dots',
                subtitle=(
                    'Opt-in: install status bridges on this host and poll '
                    'working/waiting/done for project dots + ntfy'
                ),
            )
            sw_row.set_active(bool(prof.rich_status_opt_in))
            sw_row.connect(
                'notify::active',
                lambda r, p, h=hid: self._on_host_rich_status(h, r.get_active()),
            )
            card.add(sw_row)
            self._host_switches[hid] = sw_row

    def _on_health_interval_changed(self, row, _pspec):
        self._settings.remote_health_interval_sec = int(row.get_value())
        self._settings.save()
        self._app.emit('settings-changed')

    def _on_host_rich_status(self, host_id, active):
        hosts = dict(self._settings.hosts)
        entry = dict(hosts.get(host_id) or {})
        if not entry:
            return
        # Avoid re-entrancy when rebuild sets active from saved state.
        if bool(entry.get('rich_status_opt_in', False)) == bool(active):
            return
        entry['rich_status_opt_in'] = bool(active)
        hosts[host_id] = entry
        self._settings.hosts = hosts
        self._settings.save()
        if active:
            # Install bridges on the remote (idempotent); never block GTK.
            self._set_add_host_status(
                f'Installing status bridges on {entry.get("ssh_target", host_id)}…'
            )
            import threading
            threading.Thread(
                target=self._enable_rich_status_worker,
                args=(host_id,),
                daemon=True,
                name='pm-rich-status-install',
            ).start()
        else:
            self._set_add_host_status(
                'Rich status off — project dots idle until re-enabled'
            )
            try:
                self._parent._refresh_remote_hosts()
            except Exception:
                pass

    def _enable_rich_status_worker(self, host_id):
        import remote_hooks
        from hosts import HostProfile
        raw = (self._settings.hosts or {}).get(host_id) or {}
        prof = HostProfile.from_dict({**raw, 'id': host_id})
        if prof is None:
            GLib.idle_add(self._set_add_host_status, 'Host missing')
            return
        app_dir = os.path.dirname(os.path.abspath(__file__))
        ok, msg = remote_hooks.ensure_remote_status_ready(
            prof, app_dir=app_dir,
        )

        def done():
            if ok:
                self._set_add_host_status(
                    f'Rich status ready on {prof.title()}: {msg}'
                )
                self._toast(f'Rich status enabled on {prof.title()}')
            else:
                self._set_add_host_status(f'Bridge install failed: {msg[:160]}')
                self._toast(f'Bridge install failed on {prof.title()}')
            try:
                self._parent._refresh_remote_hosts()
            except Exception:
                pass
            return False

        GLib.idle_add(done)

    def _open_host_editor(self, host_id):
        """Phase 5: edit display name, projects dir, binaries; re-probe.

        Uses Adw.Dialog (not AlertDialog) so we can set a real width — host
        settings need room for long binary paths.
        """
        from hosts import HostProfile
        raw = dict((self._settings.hosts or {}).get(host_id) or {})
        prof = HostProfile.from_dict({**raw, 'id': host_id})
        if prof is None:
            return

        dialog = Adw.Dialog()
        dialog.set_title(f'Edit host — {prof.title()}')
        # ~2× typical alert width so override paths are readable.
        if hasattr(dialog, 'set_content_width'):
            dialog.set_content_width(720)
        if hasattr(dialog, 'set_content_height'):
            dialog.set_content_height(520)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        prefs = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title='Host',
            description='Changes apply to the next spawn / health poll.',
        )
        prefs.add(group)

        name_row = Adw.EntryRow(title='Display name')
        name_row.set_text(prof.display_name or '')
        group.add(name_row)

        target_row = Adw.EntryRow(title='SSH target')
        target_row.set_text(prof.ssh_target)
        target_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        group.add(target_row)

        pdir_row = Adw.EntryRow(title='Remote projects directory')
        pdir_row.set_text(prof.remote_projects_dir or '~/.ProjectMan/projects')
        pdir_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        group.add(pdir_row)

        bin_group = Adw.PreferencesGroup(title='Harness binaries')
        prefs.add(bin_group)
        bin_widgets = {}
        for hid_bin, label in (
            ('claude', 'Claude Code'),
            ('opencode', 'OpenCode'),
            ('grok', 'Grok Build'),
        ):
            spec = prof.binary_spec(hid_bin)
            use_path = Adw.SwitchRow(
                title=f'{label}: use $PATH',
                subtitle='Off = use absolute override path below',
            )
            use_path.set_active(bool(spec.use_path))
            bin_group.add(use_path)
            path_row = Adw.EntryRow(title=f'{label} override path')
            path_row.set_text(spec.override or '')
            path_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
            bin_group.add(path_row)
            bin_widgets[hid_bin] = (use_path, path_row)

        scrolled.set_child(prefs)
        toolbar.set_content(scrolled)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(12)
        btn_box.set_margin_end(12)
        probe_btn = Gtk.Button(label='Re-probe binaries')
        cancel_btn = Gtk.Button(label='Cancel')
        save_btn = Gtk.Button(label='Save')
        save_btn.add_css_class('suggested-action')
        btn_box.append(probe_btn)
        btn_box.append(cancel_btn)
        btn_box.append(save_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(toolbar)
        outer.append(btn_box)
        dialog.set_child(outer)

        def _close(*_a):
            dialog.close()

        def _save(*_a):
            entry = dict(raw)
            entry['display_name'] = name_row.get_text().strip()
            entry['ssh_target'] = target_row.get_text().strip() or prof.ssh_target
            entry['remote_projects_dir'] = (
                pdir_row.get_text().strip() or '~/.ProjectMan/projects'
            )
            binaries = {}
            for hid_bin, (use_path, path_row) in bin_widgets.items():
                binaries[hid_bin] = {
                    'use_path': bool(use_path.get_active()),
                    'override': path_row.get_text().strip(),
                }
            entry['binaries'] = binaries
            hosts = dict(self._settings.hosts)
            hosts[host_id] = entry
            self._settings.hosts = hosts
            self._settings.save()
            self._app.emit('settings-changed')
            self._refresh_host_rows()
            self._toast(
                f'Saved host {entry.get("display_name") or entry["ssh_target"]}'
            )
            dialog.close()

        def _probe(*_a):
            self._set_add_host_status(f'Re-probing binaries on {prof.ssh_target}…')
            # Run probe off UI thread so Settings stays responsive.
            import threading

            def work():
                import remote_store
                from hosts import HostProfile as HP
                p = HP.from_dict({**raw, 'id': host_id, 'ssh_target': target_row.get_text().strip() or prof.ssh_target})
                bins = remote_store.discover_remote_binaries(p) if p else {}
                GLib.idle_add(self._apply_probe_to_editor, host_id, bins, bin_widgets)

            threading.Thread(target=work, daemon=True).start()

        cancel_btn.connect('clicked', _close)
        save_btn.connect('clicked', _save)
        probe_btn.connect('clicked', _probe)
        dialog.present(self._parent)

    def _apply_probe_to_editor(self, host_id, bins, bin_widgets):
        if not bins:
            self._set_add_host_status('No harness binaries found on host')
            self._toast('No binaries found on host')
            return False
        raw = dict((self._settings.hosts or {}).get(host_id) or {})
        binaries = dict(raw.get('binaries') or {})
        for hid_bin, path in bins.items():
            binaries[hid_bin] = {'use_path': False, 'override': path}
        raw['binaries'] = binaries
        hosts = dict(self._settings.hosts)
        hosts[host_id] = raw
        self._settings.hosts = hosts
        self._settings.save()
        if bin_widgets:
            for hid_bin, (use_path, path_row) in bin_widgets.items():
                if hid_bin in bins:
                    use_path.set_active(False)
                    path_row.set_text(bins[hid_bin])
        self._set_add_host_status(
            'Found: ' + ', '.join(f'{k}={v}' for k, v in sorted(bins.items()))
        )
        self._toast(f'Probed {len(bins)} binary(ies)')
        return False

    def _on_remove_host(self, host_id):
        hosts = dict(self._settings.hosts)
        hosts.pop(host_id, None)
        self._settings.hosts = hosts
        exp = dict(self._settings.host_section_expanded or {})
        exp.pop(host_id, None)
        self._settings.host_section_expanded = exp
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_host_rows()

    def _on_add_host_clicked(self, button):
        if getattr(self, '_add_host_busy', False):
            self._toast('Already adding a host…')
            return
        dialog = Adw.AlertDialog(
            heading='Add remote host',
            body=(
                'Enter an SSH target exactly as you would for ssh(1) '
                '(alias, FQDN, or user@host). An optional display name '
                'overrides the sidebar title.\n\n'
                'ProjectMan will use this key’s full SSH access — the same '
                'as an interactive session you already run. Ensure the host '
                'key is already accepted (ssh once manually if needed).'
            ),
        )
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('add', 'Add')
        dialog.set_response_appearance('add', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('add')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        target_row = Adw.EntryRow(title='SSH target')
        target_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        name_row = Adw.EntryRow(title='Display name (optional)')
        box.append(target_row)
        box.append(name_row)
        dialog.set_extra_child(box)

        def _on_response(d, response):
            if response != 'add':
                return
            target = target_row.get_text().strip()
            if not target:
                return
            name = name_row.get_text().strip()
            # Immediate, obvious feedback: button + status line + toast.
            self._add_host_busy = True
            if hasattr(self, '_add_host_btn') and self._add_host_btn is not None:
                self._add_host_btn.set_sensitive(False)
                self._add_host_btn.set_label('Contacting…')
            self._set_add_host_status(f'Contacting {target}… (SSH may take a few seconds)')
            self._toast(f'Contacting {target}…')
            # Defer so the toast / label can paint before blocking SSH work.
            GLib.idle_add(self._add_host, target, name)

        dialog.connect('response', _on_response)
        dialog.present(self._parent)

    def _toast(self, text, timeout=4):
        """Surface a toast on the main window (PreferencesDialog has none)."""
        try:
            self._parent._show_toast(text, timeout=timeout)
        except TypeError:
            try:
                self._parent._show_toast(text)
            except Exception:
                pass
        except Exception:
            pass

    def _set_add_host_status(self, text: str):
        if not hasattr(self, '_add_host_status') or self._add_host_status is None:
            return
        text = (text or '').strip()
        self._add_host_status.set_label(text)
        self._add_host_status.set_visible(bool(text))

    def _add_host_reset_button(self):
        self._add_host_busy = False
        if hasattr(self, '_add_host_btn') and self._add_host_btn is not None:
            self._add_host_btn.set_sensitive(True)
            self._add_host_btn.set_label('Add Host…')

    def _add_host(self, ssh_target, display_name):
        from hosts import HostProfile, new_host_id
        import remote_store
        hid = new_host_id()
        prof = HostProfile(
            id=hid,
            ssh_target=ssh_target,
            display_name=display_name or '',
            # Opt-in only — rich status hooks are not installed until enabled.
            rich_status_opt_in=False,
        )
        # Connectivity + ensure projects dir (blocking; preceded by toast)
        ok, err = remote_store.ensure_remote_projects_dir(prof)
        if not ok:
            msg = f'Could not reach {ssh_target}: {(err or "ssh failed")[:120]}'
            self._set_add_host_status(msg)
            self._toast(msg, timeout=6)
            self._add_host_reset_button()
            return False
        self._set_add_host_status(f'Connected to {prof.title()} — discovering binaries…')
        self._toast(f'Connected to {prof.title()} — finishing setup…')
        bins = remote_store.discover_remote_binaries(prof)
        # Store absolute paths (use_path=False). Non-interactive SSH often
        # lacks interactive PATH (.bashrc early-return), so bare names fail.
        binaries = {}
        for hid_bin, path in bins.items():
            binaries[hid_bin] = {'use_path': False, 'override': path}
        prof.binaries = binaries
        hosts = dict(self._settings.hosts)
        hosts[hid] = prof.to_dict()
        self._settings.hosts = hosts
        self._settings.set_section_expanded(hid, True)
        self._settings.save()
        self._app.emit('settings-changed')
        self._refresh_host_rows()
        try:
            self._parent._refresh_remote_hosts()
        except Exception:
            pass
        bin_note = ''
        if bins:
            bin_note = ' · ' + ', '.join(f'{k}={v}' for k, v in sorted(bins.items()))
        self._set_add_host_status(f'Host added: {prof.title()}{bin_note}')
        self._toast(f'Host added: {prof.title()}')
        self._add_host_reset_button()
        return False  # GLib.idle_add: do not reschedule

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
            description=(
                'Proactive background monitor for project health. '
                'Localhost projects only — remote hosts are not scanned.'
            ),
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
        dialog.select_folder(self._parent, None, self._on_folder_chosen)

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
        if not isinstance(self._settings.harnesses, dict):
            self._settings.harnesses = {}
        claude_cfg = self._settings.harnesses.get('claude')
        if not isinstance(claude_cfg, dict):
            claude_cfg = {}
            self._settings.harnesses['claude'] = claude_cfg
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
        self._provider_combo = Adw.ComboRow(title='Default Provider, Claude Code')
        self._provider_combo.set_subtitle(
            'Anthropic (native) uses your own Anthropic credentials')
        self._provider_combo.connect('notify::selected', self._on_default_provider_changed)
        self._active_provider_group.add(self._provider_combo)
        for future_title in (
            'Default Provider, Grok Build (future)',
            'Default Provider, OpenCode (future)',
        ):
            future_row = Adw.ActionRow(title=future_title)
            future_row.set_subtitle('Not configurable in ProjectMan yet')
            future_row.set_sensitive(False)
            self._active_provider_group.add(future_row)

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

        # Add Provider lives in the same group as provider rows so spacing
        # matches inter-row gaps (a separate PreferencesGroup was too large).
        self._provider_add_row = Adw.ActionRow()
        add_btn = Gtk.Button(label='Add Provider')
        add_btn.add_css_class('suggested-action')
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect('clicked', self._on_add_provider)
        self._provider_add_row.add_prefix(add_btn)

        self._provider_card_rows = []
        self._refresh_models_page()
        self._build_native_model_sections(page)

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
        # Drop Add row if present so provider rows insert above it cleanly.
        add_row = getattr(self, '_provider_add_row', None)
        if add_row is not None and add_row.get_parent() is self._providers_group:
            self._providers_group.remove(add_row)
        if isinstance(self._settings.providers, dict):
            for pid in sorted(self._settings.providers):
                prov = self._settings.providers.get(pid)
                if not isinstance(prov, dict):
                    continue
                row = self._build_provider_row(pid, prov)
                self._providers_group.add(row)
                self._provider_card_rows.append(row)
        if add_row is not None:
            self._providers_group.add(add_row)

    def _build_provider_row(self, pid, prov):
        """A slim, one-line row for a provider in the Models page. The row's
        title is the provider's display name (or pid) — this preserves the gate
        walk's ``has('Ollama')`` assertion — and activating the row (click)
        opens the full editor dialog. No tier combos or entries live here, so
        this row is never the thing being typed into."""
        name = prov.get('name') or pid
        # Subtitle shows the provider's base_url (identifying, useful) rather
        # than the pid — pid is an internal key ('provider', 'provider2', …)
        # that's meaningless to users and was being read as the row's label.
        # Empty for a freshly-added provider that has no URL yet.
        row = Adw.ActionRow(title=name, subtitle=prov.get('base_url') or '')
        models = prov.get('models') if isinstance(prov.get('models'), list) else []
        n = len([m for m in models if isinstance(m, str)])
        if n:
            count = Gtk.Label(label=f'{n} model{"s" if n != 1 else ""}')
            count.add_css_class('dim-label')
            count.set_valign(Gtk.Align.CENTER)
            row.add_suffix(count)
        # Click opens the editor. No chevron — the row is a card, not an expander.
        row.set_activatable(True)
        row.connect('activated', lambda _r, p=pid: self._open_editor(p))
        return row

    def _open_editor(self, pid):
        """Open the provider editor dialog. On close it refreshes this page
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

    def _build_native_model_sections(self, page):
        """Placeholder sections for Grok / OpenCode model ownership.

        These harnesses pick models in their own configs; PM does not list or
        edit those models here. One non-interactive row each points at that.
        """
        import harnesses
        import harness_configs
        for harness_id in ('grok', 'opencode'):
            adapter = harnesses.ADAPTERS.get(harness_id)
            display = adapter.display_name if adapter else harness_id
            cfg = harness_configs.load_harness_config(harness_id)
            shown_path = ''
            if cfg is not None:
                shown_path = harness_configs._display_path(cfg.source_path)
            group = Adw.PreferencesGroup(title=display)
            if shown_path:
                group.set_description(
                    f'Models are chosen in the harness’s own config ({shown_path}).')
            else:
                group.set_description(
                    'Models are chosen in the harness’s own config.')
            page.add(group)
            row = Adw.ActionRow(title='Managed by the harness')
            row.set_sensitive(False)
            group.add(row)


    # ------------------------------------------------------------------ #
    #  Harnesses page (B3 — minimal this phase; full doctor is P3)            #
    # ------------------------------------------------------------------ #


    #  Harnesses page (B3 — minimal this phase; full doctor is P3)            #
    # ------------------------------------------------------------------ #

    def _build_harnesses_page(self):
        import harnesses
        import harness_configs
        page = Adw.PreferencesPage(
            title='Harnesses', icon_name='applications-engineering-symbolic'
        )
        self.add(page)

        default_group = Adw.PreferencesGroup(title='General')
        page.add(default_group)

        self._harness_default_ids = list(harnesses.ADAPTERS.keys())
        labels = [harnesses.ADAPTERS[a].display_name for a in self._harness_default_ids]
        self._harness_default_combo = Adw.ComboRow(title='Default Harness')
        self._harness_default_combo.set_tooltip_text(
            'The coding harness used for new sessions. Override per project '
            'from the sidebar right-click menu.')
        self._harness_default_combo.set_model(Gtk.StringList.new(labels))
        cur = self._settings.harness_default
        self._harness_default_combo.set_selected(
            self._harness_default_ids.index(cur) if cur in self._harness_default_ids else 0)
        self._harness_default_combo.connect(
            'notify::selected', self._on_harness_default_changed)
        default_group.add(self._harness_default_combo)

        # Per-harness config: binary path + doctor-lite check.
        self._harness_binary_rows = {}
        # M-UX.8: (row, button) per harness so the bridge state can refresh after
        # an install click without rebuilding the page.
        self._bridge_rows = {}
        for harness_id in self._harness_default_ids:
            adapter = harnesses.ADAPTERS[harness_id]
            group = Adw.PreferencesGroup(title=adapter.display_name)
            page.add(group)

            cfg = self._settings.harnesses.get(harness_id, {}) if isinstance(
                self._settings.harnesses, dict) else {}
            binary_row = Adw.EntryRow(title='Binary')
            binary_row.set_text((cfg.get('binary') or '') if isinstance(cfg, dict) else '')
            binary_row.set_show_apply_button(True)
            binary_row.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
            binary_row.set_tooltip_text(
                f'Leave blank to use "{harness_id}" from PATH')
            binary_row.connect(
                'apply', lambda r, aid=harness_id: self._on_harness_binary_apply(aid, r))
            group.add(binary_row)
            self._harness_binary_rows[harness_id] = binary_row

            # Doctor-lite: <binary> --version.
            check_row = Adw.ActionRow(title='Status')
            check_row.set_subtitle('Run a check to verify the binary')
            check_btn = Gtk.Button(label='Check')
            check_btn.set_valign(Gtk.Align.CENTER)
            check_btn.add_css_class('flat')
            check_btn.connect(
                'clicked', lambda b, aid=harness_id, r=check_row: self._on_harness_doctor(aid, r))
            check_row.add_suffix(check_btn)
            group.add(check_row)

            # B2 (M-UX.13, C1/C2): per-harness account status — "is my subscription
            # connected?" answered at a glance, presence-based (the Check button
            # above stays the live probe). Contents are never read; only the
            # token file's existence/size (or, for opencode, parsed providers).
            account_line = harness_configs.account_status_line(harness_id)
            if account_line is not None:
                account_row = Adw.ActionRow(title='Account')
                account_row.set_subtitle(account_line)
                account_row.set_sensitive(False)
                group.add(account_row)

            # B1 (M-UX.8-residual / F10, C1/C5): grok reads Claude-style hooks by
            # default, which would make Claude's hook double-fire on grok events.
            # Surface the [compat.claude] hooks state read-only (the file-driven
            # behavior was invisible — a C1 defect).
            if harness_id == 'grok':
                compat_row = Adw.ActionRow(title='Claude-hooks compat')
                compat_row.set_subtitle(harness_configs.grok_compat_hooks_line())
                compat_row.set_sensitive(False)
                group.add(compat_row)

            # Status-bridge install button (only for agents that ship one).
            if harnesses.harness_bridge_source(self._app_dir(), harness_id) is not None \
                    or harness_id == 'opencode':
                bridge_row = Adw.ActionRow(title='Status bridge')
                bridge_btn = Gtk.Button()
                bridge_btn.set_valign(Gtk.Align.CENTER)
                bridge_btn.add_css_class('flat')
                bridge_btn.connect(
                    'clicked', lambda b, aid=harness_id: self._on_install_bridge(aid))
                bridge_row.add_suffix(bridge_btn)
                group.add(bridge_row)
                # M-UX.8 (C5): reflect the bridge's ACTUAL installed state via the
                # F12a manifest rather than always saying "Install bridge".
                self._bridge_rows[harness_id] = (bridge_row, bridge_btn)
                self._refresh_bridge_row(harness_id)

    @staticmethod
    def _app_dir():
        return os.path.dirname(os.path.abspath(__file__))

    def _on_harness_default_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._harness_default_ids):
            self._settings.harness_default = self._harness_default_ids[idx]
            self._save_and_notify()

    def _on_harness_binary_apply(self, harness_id, row):
        harnesses_cfg = dict(self._settings.harnesses) if isinstance(
            self._settings.harnesses, dict) else {}
        entry = dict(harnesses_cfg.get(harness_id) or {}) if isinstance(
            harnesses_cfg.get(harness_id), dict) else {}
        value = row.get_text().strip()
        entry['binary'] = value
        harnesses_cfg[harness_id] = entry
        self._settings.harnesses = harnesses_cfg
        if harness_id == 'claude':
            # Keep the legacy claude_binary key in sync. Without this, clearing
            # the row would leave a stale legacy value that resolved_claude_binary
            # falls back to — the clear would silently not take effect.
            self._settings.claude_binary = value
        self._save_and_notify()

    def _on_harness_doctor(self, harness_id, row):
        import harnesses
        ok, detail = harnesses.harness_doctor(self._settings, harness_id)
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

    def _refresh_bridge_row(self, harness_id):
        """Set the bridge button label + row subtitle from the manifest state
        (M-UX.8/C5). Called at build and after every install click."""
        import harnesses
        entry = self._bridge_rows.get(harness_id)
        if entry is None:
            return
        row, btn = entry
        state = harnesses.bridge_state(self._app_dir(), harness_id)
        label, subtitle = harnesses.bridge_button_labels(state)
        btn.set_label(label)
        row.set_subtitle(subtitle)

    def _on_install_bridge(self, harness_id):
        import harnesses
        result = harnesses.install_harness_bridge(self._app_dir(), harness_id)
        msgs = {
            'installed': 'Status bridge installed',
            'already': 'Status bridge already up to date',
            'missing-source': 'Bridge source not found in the app directory',
            'no-bridge': 'This harness has no status bridge',
            'error': 'Failed to install the status bridge',
        }
        toast = Adw.Toast.new(msgs.get(result, result))
        toast.set_timeout(3)
        self.add_toast(toast)
        # C5: the button label must now reflect the post-install state.
        self._refresh_bridge_row(harness_id)

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