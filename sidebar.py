from datetime import datetime

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GLib, GObject, Gdk, Pango

import agents
from model import ResourceReader
from models import FOLLOW_DEFAULT, NATIVE_LABEL, provider_label


class Sidebar(Gtk.Box):
    __gsignals__ = {
        'project-activated':    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'session-activated':    (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'project-archive':      (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-deactivate':   (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-new-session':  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-zellij':       (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-ntfy-toggle':  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-rename':       (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'show-archive-window':  (GObject.SignalFlags.RUN_FIRST, None, ()),
        'show-settings':        (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-create':       (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'show-paa-window':      (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-haiku-check':  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-model-change': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, store, history, watcher, version='', settings=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class('pm-sidebar')
        self._store = store
        # ``history`` retained only for back-compat construction; the expander
        # now pulls sessions through the per-project adapter (A1), not the
        # HistoryReader directly. ``settings`` lets each row resolve its
        # effective agent so the seam is load-bearing.
        self._history = history
        self._settings = settings
        self._watcher = watcher
        self._rows = {}
        self._new_project_row = None
        self._active_only = False
        self._filter_text = ''
        # Per-project model menu state, pushed in via set_model_options().
        self._model_options = []
        self._model_overrides = {}
        self._global_model_label = NATIVE_LABEL

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(6)
        btn_row.set_margin_end(8)
        btn_row.set_margin_bottom(2)

        add_btn = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_btn.add_css_class('flat')
        add_btn.add_css_class('circular')
        add_btn.set_tooltip_text('New Project')
        add_btn.connect('clicked', self._on_add_project)
        btn_row.append(add_btn)

        self._paa_btn = Gtk.Button()
        # FB-8 (C9): a BUNDLED symbolic icon, not a bare U+2728 Gtk.Label \u2014 the
        # glyph rendered as a Unicode tofu box on hosts with no emoji font (the
        # withheld round-2 finding). Image.new_from_icon_name resolves
        # pm-sparkle-symbolic from the app's icon search path (main.py adds
        # ./icons), inheriting the button's foreground via currentColor.
        self._paa_btn_icon = Gtk.Image.new_from_icon_name('pm-sparkle-symbolic')
        self._paa_btn.set_child(self._paa_btn_icon)
        self._paa_btn.add_css_class('flat')
        self._paa_btn.add_css_class('circular')
        self._paa_btn.set_tooltip_text('Projects Admin Agent')
        self._paa_btn.connect('clicked', lambda b: self.emit('show-paa-window'))
        btn_row.append(self._paa_btn)
        # The count/scanning indicator that used to ride the button's text label
        # now lives in a small adjacent label (the icon child carries no text).
        self._paa_count_label = Gtk.Label()
        self._paa_count_label.add_css_class('caption')
        self._paa_count_label.add_css_class('dim-label')
        self._paa_count_label.add_css_class('pm-paa-count')
        self._paa_count_label.set_visible(False)
        btn_row.append(self._paa_count_label)
        self._paa_count = 0
        self._paa_scanning = False

        self.append(btn_row)

        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._listbox = Gtk.ListBox()
        self._listbox.add_css_class('navigation-sidebar')
        self._listbox.connect('row-activated', self._on_row_activated)
        self._listbox.set_filter_func(self._filter_row)
        self._scrolled.set_child(self._listbox)
        self.append(self._scrolled)

        self._count_label = Gtk.Label()
        self._count_label.add_css_class('dim-label')
        self._count_label.add_css_class('caption')
        self._count_label.set_margin_top(4)
        self._count_label.set_margin_bottom(0)
        self.append(self._count_label)

        # "Active Only" toggle — hides inactive projects to reduce clutter
        self._active_toggle = Gtk.ToggleButton(label='Active Only')
        self._active_toggle.add_css_class('flat')
        self._active_toggle.add_css_class('pm-sidebar-btn')
        self._active_toggle.set_margin_start(8)
        self._active_toggle.set_margin_end(8)
        self._active_toggle.set_margin_top(4)
        self._active_toggle.set_margin_bottom(0)
        self._active_toggle.connect('toggled', self._on_active_toggled)
        self.append(self._active_toggle)

        # "Archived Projects" opens popup window
        archive_btn = Gtk.Button(label='Archived Projects')
        archive_btn.add_css_class('flat')
        archive_btn.add_css_class('pm-sidebar-btn')
        archive_btn.set_margin_start(8)
        archive_btn.set_margin_end(8)
        archive_btn.set_margin_top(2)
        archive_btn.set_margin_bottom(4)
        archive_btn.connect('clicked', lambda b: self.emit('show-archive-window'))
        self.append(archive_btn)

        self._resource_bar = ResourceBar(
            on_settings_clicked=lambda: self.emit('show-settings'),
            version=version,
        )
        self.append(self._resource_bar)

        self._populate()

    def _filter_row(self, row):
        if isinstance(row, ProjectRow):
            if self._active_only and row._process_state not in ('attached', 'detached'):
                return False
            if self._filter_text and self._filter_text not in row._project.name.lower():
                return False
        return True

    def set_filter_text(self, text):
        self._filter_text = text.lower()
        self._listbox.invalidate_filter()

    def _populate(self):
        # Preserve the in-progress new-project entry across rebuilds
        pending_row = self._new_project_row
        if pending_row is not None:
            self._listbox.remove(pending_row)
            self._new_project_row = None
        # Preserve process running state across rebuilds
        running_state = {path: row._process_state for path, row in self._rows.items()}
        # Preserve the live running-agent (C5) across rebuilds too, so a refresh
        # doesn't drop the mismatch subtitle of a still-running session.
        running_agent = {path: row._running_agent for path, row in self._rows.items()}

        self._rows.clear()
        while True:
            row = self._listbox.get_row_at_index(0)
            if row is None:
                break
            self._listbox.remove(row)

        for proj in self._store.load_projects():
            row = ProjectRow(proj, self._history, self._watcher,
                             settings=self._settings)
            if proj.path in running_state:
                row._process_state = running_state[proj.path]
                row.update_status()
            if running_agent.get(proj.path) is not None:
                row.set_running_agent(running_agent[proj.path])
            row.connect('session-activated',
                        lambda r, p, sid, pp=proj.path: self.emit('session-activated', pp, sid))
            row.connect('project-archive',
                        lambda r, p=proj.path: self.emit('project-archive', p))
            row.connect('project-deactivate',
                        lambda r, p=proj.path: self.emit('project-deactivate', p))
            row.connect('project-new-session',
                        lambda r, p=proj.path: self.emit('project-new-session', p))
            row.connect('project-zellij',
                        lambda r, p=proj.path: self.emit('project-zellij', p))
            row.connect('project-ntfy-toggle',
                        lambda r, p=proj.path: self.emit('project-ntfy-toggle', p))
            row.connect('project-haiku-check',
                        lambda r, p=proj.path: self.emit('project-haiku-check', p))
            row.connect('project-rename',
                        lambda r, new_name, p=proj.path: self.emit('project-rename', p, new_name))
            row.connect('project-model-change',
                        lambda r, mid, p=proj.path: self.emit('project-model-change', p, mid))
            row.set_model_options(
                self._model_options,
                self._model_overrides.get(proj.path, FOLLOW_DEFAULT),
                self._global_model_label,
            )
            self._listbox.append(row)
            self._rows[proj.path] = row

        if pending_row is not None:
            self._new_project_row = pending_row
            self._listbox.prepend(pending_row)
            GLib.idle_add(lambda: pending_row._entry.grab_focus() and False)

        self._update_count_label()

    def _on_row_activated(self, listbox, row):
        if isinstance(row, ProjectRow):
            self.emit('project-activated', row._project.path)

    def _on_active_toggled(self, button):
        self._active_only = button.get_active()
        self._listbox.invalidate_filter()
        self._update_count_label()

    def _update_count_label(self):
        if self._active_only:
            n = sum(1 for row in self._rows.values()
                    if row._process_state in ('attached', 'detached'))
            self._count_label.set_label(f'{n} active')
        else:
            n = len(self._rows)
            self._count_label.set_label(f'{n} projects')

    def refresh_status(self):
        for row in self._rows.values():
            row.update_status()

    def refresh(self):
        self._populate()

    def set_active_only(self, active):
        self._active_toggle.set_active(active)

    def select_project(self, path):
        if path in self._rows:
            self._listbox.select_row(self._rows[path])

    def set_project_state(self, path, state: str, is_zellij: bool = None):
        if path in self._rows:
            self._rows[path].set_process_state(state, is_zellij=is_zellij)
            if self._active_only:
                self._listbox.invalidate_filter()
            self._update_count_label()

    def set_running_agent(self, path, agent_id):
        """Push the live child's actual agent onto a row (C5). ``None`` clears
        it. No-op for an unknown path so window.py can fire unconditionally."""
        if path in self._rows:
            self._rows[path].set_running_agent(agent_id)

    def flash_sweeper_caught(self, path, duration_ms=2000):
        """Briefly italicize the project name to signal that the polling
        sweeper (window._sweep_dead_terminals) had to catch a missed exit
        that our pidfd watch didn't fire on. Visual debugging aid; remove
        once vte's reaper / glib child-watch path is fully replaced."""
        if path not in self._rows:
            return
        label = self._rows[path]._name_label
        label.add_css_class('sweeper-flash')
        def _clear():
            label.remove_css_class('sweeper-flash')
            return False
        GLib.timeout_add(duration_ms, _clear)

    def set_ntfy_enabled(self, enabled):
        for row in self._rows.values():
            row.update_ntfy_visibility(enabled)

    def set_settings(self, settings):
        """Push updated settings so rows re-resolve their effective provider.

        Called from window.apply_settings after a model override change so the
        per-row caps gating (A5) and session source (A1) follow the new
        effective provider.
        """
        self._settings = settings
        for row in self._rows.values():
            row.set_settings(settings)

    def set_model_options(self, options, overrides, global_label):
        """Push provider/model menu options to every project row.

        options: list of (model_id, label). overrides: {path: 'provider/model'}.
        global_label: label of the global default model.
        """
        self._model_options = list(options)
        self._model_overrides = dict(overrides)
        self._global_model_label = global_label
        for path, row in self._rows.items():
            row.set_model_options(
                options, overrides.get(path, FOLLOW_DEFAULT), global_label)

    def get_ntfy_active_paths(self):
        return {path for path, row in self._rows.items()
                if row._ntfy_action.get_state().get_boolean()}

    def start_polling(self):
        self._resource_bar.start_polling()

    def set_paa_pending_count(self, count):
        """Update PAA button label and tooltip only (no throb change)."""
        self._paa_count = count
        self._update_paa_label()
        if not self._paa_scanning:
            if count > 0:
                self._paa_btn.set_tooltip_text(
                    f'Projects Admin Agent \u2014 {count} pending'
                )
            else:
                self._paa_btn.set_tooltip_text('Projects Admin Agent')
        if count == 0:
            self.stop_paa_throb()

    def set_paa_scanning(self, names):
        """Show/hide scanning indicator on the sparkle button."""
        self._paa_scanning = bool(names)
        self._update_paa_label()
        if names:
            self._paa_btn.set_tooltip_text(f'PAA \u2014 scanning: {names}')
        elif self._paa_count > 0:
            self._paa_btn.set_tooltip_text(
                f'Projects Admin Agent \u2014 {self._paa_count} pending'
            )
        else:
            self._paa_btn.set_tooltip_text('Projects Admin Agent')

    def _update_paa_label(self):
        """Rebuild the adjacent count/scanning indicator from count + scanning
        state (FB-8). The sparkle itself is now a fixed bundled icon; only the
        count + scanning glyph are dynamic, so they render in the adjacent count
        label. The label is hidden when there is nothing to show (no pending
        findings, not scanning), so a clean state shows just the icon."""
        parts = []
        if self._paa_count > 0:
            parts.append(str(self._paa_count))
        if self._paa_scanning:
            parts.append('\u27f3')  # ⟳
        text = ' '.join(parts)
        self._paa_count_label.set_label(text)
        self._paa_count_label.set_visible(bool(text))

    def start_paa_throb(self):
        """Start the golden glow animation."""
        self._paa_btn.add_css_class('paa-btn-throb')

    def stop_paa_throb(self):
        """Stop the golden glow animation."""
        self._paa_btn.remove_css_class('paa-btn-throb')

    def _on_add_project(self, button):
        if self._new_project_row is not None:
            self._new_project_row._entry.grab_focus()
            return
        row = NewProjectEntryRow(
            on_commit=self._commit_new_project,
            on_cancel=self._cancel_new_project,
        )
        self._new_project_row = row
        self._listbox.prepend(row)

    def _commit_new_project(self, name):
        row = self._new_project_row
        self._new_project_row = None
        if row is not None:
            self._listbox.remove(row)
        self.emit('project-create', name)

    def _cancel_new_project(self):
        if self._new_project_row is None:
            return
        self._listbox.remove(self._new_project_row)
        self._new_project_row = None


class NewProjectEntryRow(Gtk.ListBoxRow):
    """Inline entry row for creating a new project directory."""
    def __init__(self, on_commit, on_cancel):
        super().__init__()
        self.set_selectable(False)
        self.set_activatable(False)
        self._on_commit = on_commit
        self._on_cancel = on_cancel

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name('folder-new-symbolic')
        box.append(icon)

        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text('Project name\u2026')
        self._entry.set_hexpand(True)
        self._entry.connect('activate', self._on_activate)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._entry.add_controller(key_ctrl)

        box.append(self._entry)
        self.set_child(box)
        GLib.idle_add(lambda: self._entry.grab_focus() and False)

    def _on_activate(self, entry):
        name = entry.get_text().strip()
        if name and '/' not in name and not name.startswith('.'):
            self._on_commit(name)

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._on_cancel()
            return True
        # Consume Enter so it doesn't bubble to ListBox and activate a project row
        # after _on_activate has already committed and removed this row.
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return True
        return False


class ProjectRow(Gtk.ListBoxRow):
    __gsignals__ = {
        'session-activated':  (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'project-archive':    (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-deactivate': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-new-session': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-zellij':     (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-ntfy-toggle': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-haiku-check': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-rename':     (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-model-change': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, project, history, watcher, settings=None):
        super().__init__()
        self._project = project
        self._history = history
        self._settings = settings
        self._watcher = watcher
        self._expanded = False
        self._sessions_loaded = False
        self._process_state = 'inactive'
        self._is_zellij = False
        self._new_session_row = None
        # The agent id the LIVE child is actually running (C5 truth), pushed in
        # from window.py's process-started/exited flow. None when nothing runs.
        # When it disagrees with the configured/effective agent (a restored
        # saved-agent-wins session, A2), the subtitle leads with what's running.
        self._running_agent = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        top.set_margin_start(4)
        top.set_margin_end(8)
        top.set_margin_top(4)
        top.set_margin_bottom(4)

        self._arrow = Gtk.Button()
        self._arrow.add_css_class('flat')
        self._arrow.add_css_class('expand-arrow')
        self._arrow.set_valign(Gtk.Align.CENTER)
        self._arrow_label = Gtk.Label(label='\u203a')
        self._arrow.set_child(self._arrow_label)
        self._arrow.connect('clicked', self._on_expand_clicked)
        top.append(self._arrow)

        self._status_dot = Gtk.Box()
        self._status_dot.add_css_class('status-dot')
        self._status_dot.add_css_class('status-stopped')
        self._status_dot.set_valign(Gtk.Align.CENTER)
        top.append(self._status_dot)

        # Name + an effective-agent subtitle (B3). Vertical so the subtitle
        # sits under the name; the box carries the hexpand the name used to.
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name_box.set_hexpand(True)
        name_box.set_valign(Gtk.Align.CENTER)
        self._name_label = Gtk.Label(label=project.name)
        self._name_label.set_halign(Gtk.Align.START)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(self._name_label)
        self._subtitle_label = Gtk.Label()
        self._subtitle_label.set_halign(Gtk.Align.START)
        self._subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._subtitle_label.add_css_class('dim-label')
        self._subtitle_label.add_css_class('caption')
        self._subtitle_label.add_css_class('pm-agent-subtitle')
        self._subtitle_label.set_visible(False)
        name_box.append(self._subtitle_label)
        top.append(name_box)

        self._rename_entry = Gtk.Entry()
        self._rename_entry.set_hexpand(True)
        self._rename_entry.set_visible(False)
        self._rename_entry.connect('activate', self._on_rename_activate)
        rename_key = Gtk.EventControllerKey.new()
        rename_key.connect('key-pressed', self._on_rename_key)
        self._rename_entry.add_controller(rename_key)
        rename_focus = Gtk.EventControllerFocus.new()
        rename_focus.connect('leave', lambda c: self._exit_rename_mode())
        self._rename_entry.add_controller(rename_focus)
        top.append(self._rename_entry)

        # Action buttons — visible on hover
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions_box.add_css_class('project-row-actions')

        self._deactivate_btn = Gtk.Button.new_from_icon_name('media-playback-stop-symbolic')
        self._deactivate_btn.add_css_class('flat')
        self._deactivate_btn.set_valign(Gtk.Align.CENTER)
        self._deactivate_btn.set_tooltip_text('Deactivate session')
        self._deactivate_btn.set_sensitive(False)  # only enabled when process running
        self._deactivate_btn.connect('clicked', lambda b: self._show_confirm_popover(b, lambda: self.emit('project-deactivate')))
        actions_box.append(self._deactivate_btn)

        top.append(actions_box)

        outer.append(top)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._session_listbox = Gtk.ListBox()
        self._session_listbox.add_css_class('navigation-sidebar')
        self._session_listbox.connect('row-activated', self._on_session_activated)
        self._revealer.set_child(self._session_listbox)
        outer.append(self._revealer)

        self._setup_context_menu()
        self._apply_caps()

    # --- agent seam (A1 sessions, A5 caps gating) -------------------------

    def _adapter(self):
        """The effective agent adapter for this project (claude if unknown).

        Resolved through the seam so the expander, the session-resume action,
        and the Model submenu all degrade on the adapter's declared caps rather
        than assuming claude. Falls back to the claude adapter when settings
        weren't injected (defensive — keeps headless/legacy construction sane).
        """
        agent_id = (self._settings.effective_agent(self._project.path)
                    if self._settings is not None else 'claude')
        # F9: thread settings into get_adapter so a named-but-missing agent's
        # caps-gating reflects the M-P3.2 fallback (agent_default → first
        # available), not a hardcoded claude. With settings the row gates on the
        # adapter that will ACTUALLY run for this project.
        return agents.get_adapter(agent_id, self._settings)

    def _caps(self):
        return self._adapter().caps

    def _remap_idle_to_done(self):
        """Whether an attached row's watcher-'idle' should render as 'done'.

        Gated on the effective adapter's ``caps.rich_status`` (its first
        consumer): a rich-status agent (claude, opencode — both bridged) with
        no status file yet is a genuinely just-started/finished state, so the
        historic green remap stands. A ``rich_status=False`` agent has no
        bridge at all — remapping would pin a fake green "work finished" dot
        for the entire run; it keeps the honest dim 'idle' instead. If adapter
        resolution fails for any reason, preserve the historic remap — the dot
        path must never throw.
        """
        try:
            return self._adapter().caps.rich_status
        except Exception:
            return True

    def set_settings(self, settings):
        """Re-bind settings and re-apply caps gating (effective provider changed)."""
        self._settings = settings
        self._apply_caps()
        self._rebuild_popover()

    def _apply_caps(self):
        """Show/hide caps-gated menu entries for the effective adapter (A5).

        * Model submenu     — caps.model_select
        * History expander  — caps.sessions (the expand arrow + new-session row)
        * Session resume     — caps.resume_by_id (gated where the row is built)

        Idempotent: safe to call after construction and on every settings push.
        """
        caps = self._caps()
        # Model submenu visibility.
        self._set_menu_item_present('Model', caps.model_select,
                                    self._insert_model_submenu)
        # History expander: hide the arrow entirely when the agent can't
        # enumerate sessions, so there's no empty dropdown to open.
        if hasattr(self, '_arrow'):
            self._arrow.set_visible(caps.sessions)
        self._update_subtitle()

    def _subtitle_text(self):
        """Pure builder for the row subtitle string. Returns the text to show,
        or ``None`` when the row should stay clean (no subtitle).

        Claude Code is the sole harness, so the harness part is always the
        default and is hidden unless a provider is pinned. Two shapes:

          * NO live mismatch — the running harness is absent OR equals the
            configured one (always true now): ``None`` for a plain native row,
            ``<HarnessDisplay> · <ProviderLabel>`` when a provider is pinned.
          * LIVE mismatch — a child is running a DIFFERENT harness than the one
            configured (unreachable with a single harness, but kept as a
            defensive guard): lead with what is ACTUALLY running, naming the
            next: ``<RunningDisplay> (next: <ConfiguredDisplay>)`` (+ the
            provider suffix). Always shown.
        """
        if self._settings is None:
            return None
        agent_id = self._settings.effective_agent(self._project.path)
        provider = self._settings.effective_provider(self._project.path)
        running = self._running_agent
        mismatch = running is not None and running != agent_id
        if mismatch:
            head = (f'{self._harness_display(running)} '
                    f'(next: {self._harness_display(agent_id)})')
        else:
            if not provider:
                return None
            head = self._harness_display(agent_id)
        parts = [head]
        if provider:
            parts.append(provider_label(self._settings.providers, provider))
        return ' · '.join(parts)

    @staticmethod
    def _harness_display(agent_id):
        adapter = agents.ADAPTERS.get(agent_id)
        return adapter.display_name if adapter else agent_id

    def _update_subtitle(self):
        """Render the subtitle from the pure builder (B3 + C5).

        Surfaces a non-default configuration — or a live agent mismatch — without
        opening the menus; a plain default/native row stays clean.
        """
        if not hasattr(self, '_subtitle_label'):
            return
        text = self._subtitle_text()
        if text is None:
            self._subtitle_label.set_visible(False)
            return
        self._subtitle_label.set_text(text)
        self._subtitle_label.set_visible(True)

    def set_running_agent(self, agent_id):
        """Record the agent the live child is actually running (C5) and refresh
        the subtitle. ``None`` clears it (the session ended)."""
        self._running_agent = agent_id
        self._update_subtitle()

    def _set_menu_item_present(self, label, present, inserter):
        """Ensure a top-level menu item with ``label`` is present/absent.

        Generic helper mirroring update_ntfy_visibility's add/remove dance so
        caps gating reuses one code path. ``inserter`` re-adds the item when it
        must reappear (its position/submodel is caller-defined).
        """
        idx = None
        for i in range(self._menu.get_n_items()):
            v = self._menu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
            if v and v.get_string() == label:
                idx = i
                break
        if present and idx is None:
            inserter()
            self._rebuild_popover()
        elif not present and idx is not None:
            self._menu.remove(idx)
            self._rebuild_popover()

    def _insert_model_submenu(self):
        """Re-attach the Model submenu (used by caps gating to restore it).

        Appended at the end on re-add (the initial build placed it mid-menu);
        the position only shifts on a low→high caps transition, which is rare
        and cosmetic.
        """
        self._menu.append_submenu('Model', self._model_submenu)

    def _setup_context_menu(self):
        self._menu = Gio.Menu()
        self._menu.append('New Session',        'row.new-session')
        self._menu.append('New Zellij Session', 'row.zellij')
        self._menu.append('Haiku Check',        'row.haiku-check')
        self._model_submenu = Gio.Menu()
        self._menu.append_submenu('Model', self._model_submenu)
        self._menu.append('Rename',             'row.rename')
        self._menu.append('Archive',            'row.archive')

        ag = Gio.SimpleActionGroup()

        def _add(name, signal_name):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate',
                           lambda a, p, sn=signal_name: self.emit(sn))
            ag.add_action(action)
            return action

        self._new_session_action = _add('new-session', 'project-new-session')
        _add('zellij',       'project-zellij')
        _add('haiku-check',  'project-haiku-check')
        _add('archive',      'project-archive')

        rename_action = Gio.SimpleAction.new('rename', None)
        rename_action.connect('activate', lambda a, p: self._enter_rename_mode())
        ag.add_action(rename_action)

        self._ntfy_action = Gio.SimpleAction.new_stateful(
            'ntfy-done', None, GLib.Variant('b', False)
        )
        self._ntfy_action.connect('activate', self._on_ntfy_activate)
        ag.add_action(self._ntfy_action)

        # Per-project provider selection (the sidebar Model menu is a provider
        # picker). A single stateful 's' action lets GTK render the submenu as
        # radio items with the active one checked.
        self._model_action = Gio.SimpleAction.new_stateful(
            'set-model', GLib.VariantType.new('s'),
            GLib.Variant('s', FOLLOW_DEFAULT),
        )
        self._model_action.connect('activate', self._on_model_select)
        ag.add_action(self._model_action)

        self.insert_action_group('row', ag)
        self._rebuild_popover()

        click = Gtk.GestureClick.new()
        click.set_button(3)
        click.connect('pressed', self._on_right_click)
        self.add_controller(click)

    def _on_ntfy_activate(self, action, param):
        new_state = not action.get_state().get_boolean()
        action.set_state(GLib.Variant('b', new_state))
        self.emit('project-ntfy-toggle')

    def update_ntfy_visibility(self, enabled):
        ntfy_label = 'Ntfy on Done'
        present = False
        for i in range(self._menu.get_n_items()):
            v = self._menu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
            if v and v.get_string() == ntfy_label:
                present = True
                break
        if enabled and not present:
            self._menu.insert(2, ntfy_label, 'row.ntfy-done')
            self._rebuild_popover()
        elif not enabled and present:
            for i in range(self._menu.get_n_items()):
                v = self._menu.get_item_attribute_value(i, 'label', GLib.VariantType('s'))
                if v and v.get_string() == ntfy_label:
                    self._menu.remove(i)
                    break
            self._rebuild_popover()

    def _rebuild_popover(self):
        """(Re)create the context-menu popover after the menu model changes."""
        self._popover = Gtk.PopoverMenu.new_from_model(self._menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)

    def set_model_options(self, options, current, global_label):
        """Populate the 'Model' submenu (a per-project PROVIDER picker).

        options: list of (provider_id, label); provider_id '' is
                 ``Anthropic (native)``.
        current: the active selection — ``FOLLOW_DEFAULT``, a provider_id, or
                 ``''`` (pinned to native).
        global_label: label of the global default provider, shown on the
                 Default entry (fallback for settings-less/legacy rows).

        P3.5f (C2, David's second reveal): the "Default (…)" label is PER-ROW —
        computed from THIS row's effective provider, not the single global label
        the window pushes (which a per-project-override row would wear as the
        global default's story). ``global_label`` is the fallback for
        settings-less/legacy rows.
        """
        self._model_submenu.remove_all()
        # reveal-3 item 1 (G1, C2/C4): a row with no override and a native default
        # showed BOTH 'Default (Anthropic (native))' AND a bare
        # 'Anthropic (native)' — the native sentinel (index 0, id '') duplicates
        # the Default item's resolved story. We compute the Default story ONCE
        # and suppress any option that merely restates it. FOLLOW_DEFAULT (track
        # the default) and an explicit pin stay distinct choices; we only drop a
        # redundant pin the user hasn't taken — a LIVE pin (current == id) stays
        # visible and checked.
        default_story = self._default_label(global_label)
        default_item = Gio.MenuItem.new(f'Default ({default_story})', None)
        default_item.set_action_and_target_value(
            'row.set-model', GLib.Variant('s', FOLLOW_DEFAULT))
        self._model_submenu.append_item(default_item)
        for provider_id, label in options:
            if label == default_story and current != provider_id:
                continue
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                'row.set-model', GLib.Variant('s', provider_id))
            self._model_submenu.append_item(item)
        self._model_action.set_state(GLib.Variant('s', current))
        self._rebuild_popover()

    def _default_label(self, fallback):
        """The "Default (…)" provider-story label for THIS row's effective
        provider.

        P3.5f (C2): a per-project-override row must NOT wear the global
        default's story. Resolve per-row via ``settings.effective_provider`` +
        ``models.provider_label``. Settings-less/legacy rows — or any failure —
        fall back to the global label, so the menu path never throws.
        """
        if self._settings is None:
            return fallback
        try:
            pid = self._settings.effective_provider(self._project.path)
            return provider_label(self._settings.providers, pid)
        except Exception:
            return fallback

    def _on_model_select(self, action, value):
        action.set_state(value)
        self.emit('project-model-change', value.get_string())

    def _on_right_click(self, gesture, n_press, x, y):
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _show_confirm_popover(self, trigger_btn, action_fn):
        popover = Gtk.Popover()
        popover.set_parent(trigger_btn)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_has_arrow(True)
        popover.connect('closed', lambda p: p.unparent())
        confirm_btn = Gtk.Button(label='Confirm')
        confirm_btn.add_css_class('destructive-action')
        confirm_btn.set_margin_top(4)
        confirm_btn.set_margin_bottom(4)
        confirm_btn.set_margin_start(4)
        confirm_btn.set_margin_end(4)
        confirm_btn.connect('clicked', lambda _b: (action_fn(), popover.popdown()))
        popover.set_child(confirm_btn)
        popover.popup()

    def _on_expand_clicked(self, button):
        self._expanded = not self._expanded
        self._revealer.set_reveal_child(self._expanded)
        self._arrow_label.set_label('\u2304' if self._expanded else '\u203a')
        if self._expanded and not self._sessions_loaded:
            self._load_sessions()
            self._sessions_loaded = True

    def _load_sessions(self):
        self._new_session_row = NewSessionRow()
        self._new_session_row.set_sensitive(not self._is_zellij)
        self._new_session_row.set_activatable(not self._is_zellij)
        self._session_listbox.append(self._new_session_row)
        # A1: sessions come through the per-project adapter's list_sessions
        # (SessionRefs), NOT the HistoryReader directly. The adapter is the
        # seam; for claude it wraps HistoryReader, but the row never knows that.
        # resume-by-id rows are only enumerated when the adapter supports it
        # (A5: caps.resume_by_id) — otherwise only the New Session entry shows.
        if not self._caps().resume_by_id:
            return
        refs = self._adapter().list_sessions(self._project, self._settings)
        for i, ref in enumerate(refs):
            self._session_listbox.append(SessionHistoryRow(ref, is_default=(i == 0)))

    def _on_session_activated(self, listbox, row):
        if isinstance(row, NewSessionRow):
            self.emit('project-new-session')
        elif isinstance(row, SessionHistoryRow):
            self.emit('session-activated', self._project.path, row._ref.id)

    def set_process_state(self, state: str, is_zellij: bool = None):
        """state: 'inactive' | 'attached' | 'detached'"""
        self._process_state = state
        if is_zellij is not None:
            self._is_zellij = is_zellij
        elif state == 'detached':
            self._is_zellij = True
        elif state == 'inactive':
            self._is_zellij = False
        self._deactivate_btn.set_sensitive(state == 'attached')
        self._new_session_action.set_enabled(not self._is_zellij)
        if self._new_session_row is not None:
            self._new_session_row.set_sensitive(not self._is_zellij)
            self._new_session_row.set_activatable(not self._is_zellij)
        if state == 'detached':
            self._name_label.add_css_class('project-row-detached')
            self._name_label.set_tooltip_text('Detached zellij session')
        else:
            self._name_label.remove_css_class('project-row-detached')
            self._name_label.set_tooltip_text('')
        self.update_status()

    def update_status(self):
        # Clear all classes, including legacy names (status-active, status-notification)
        # to safely migrate any widget that had them applied before this version.
        for s in ('status-stopped', 'status-idle', 'status-active',
                  'status-done', 'status-working', 'status-waiting', 'status-notification'):
            self._status_dot.remove_css_class(s)
        if self._process_state == 'inactive':
            self._status_dot.add_css_class('status-stopped')
            return
        if self._process_state == 'detached':
            self._status_dot.add_css_class('status-idle')
            return
        # attached: apply live status; default to done if no file exists yet —
        # but only for agents whose caps declare rich_status (a bridgeless
        # agent must not fake a green "finished" dot; see _remap_idle_to_done).
        status = self._watcher.get_project_status(self._project)
        if status == 'idle' and self._remap_idle_to_done():
            status = 'done'
        self._status_dot.add_css_class(f'status-{status}')

    def _enter_rename_mode(self):
        self._rename_entry.set_text(self._project.name)
        self._name_label.set_visible(False)
        self._rename_entry.set_visible(True)
        # Defer grab_focus so the popover menu (which is still closing) doesn't
        # restore focus to its previous holder and trigger our focus-leave handler.
        def _focus():
            self._rename_entry.grab_focus()
            self._rename_entry.select_region(0, -1)
            return False
        GLib.idle_add(_focus)

    def _exit_rename_mode(self):
        self._rename_entry.set_visible(False)
        self._name_label.set_visible(True)

    def _on_rename_activate(self, entry):
        name = entry.get_text().strip()
        valid = (name and '/' not in name
                 and not name.startswith('.') and name != self._project.name)
        self._exit_rename_mode()
        if valid:
            self.emit('project-rename', name)

    def _on_rename_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._exit_rename_mode()
            return True
        return False


class NewSessionRow(Gtk.ListBoxRow):
    """Top entry in the session history dropdown — starts a fresh session."""
    def __init__(self):
        super().__init__()
        self.add_css_class('session-history-row')

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name('list-add-symbolic')
        icon.set_pixel_size(12)
        box.append(icon)

        label = Gtk.Label(label='New Session\u2026')
        label.set_halign(Gtk.Align.START)
        label.add_css_class('session-title')
        box.append(label)

        self.set_child(box)


class SessionHistoryRow(Gtk.ListBoxRow):
    """A restorable past session, rendered from an ``agents.SessionRef`` (A1).

    The row reads ``ref.id``/``ref.title``/``ref.last_active`` \u2014 the canonical
    sessions contract \u2014 not the old Claude-specific ``Session.session_id``. The
    adapter is the only thing that knows how to interpret ``ref.id``.
    """
    def __init__(self, ref, is_default=False):
        super().__init__()
        self._ref = ref
        self.add_css_class('session-history-row')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        title_text = ref.title[:40] if ref.title else '(untitled)'
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label=title_text)
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        title.add_css_class('session-title')
        title_row.append(title)
        if is_default:
            badge = Gtk.Label(label='\u21a9 continue')
            badge.add_css_class('session-default-badge')
            title_row.append(badge)
        box.append(title_row)

        try:
            dt = datetime.fromtimestamp(ref.last_active / 1000)
            ts_text = dt.strftime('%b %d, %H:%M')
        except (ValueError, OSError):
            ts_text = ''
        ts = Gtk.Label(label=ts_text)
        ts.set_halign(Gtk.Align.START)
        ts.add_css_class('dim-label')
        ts.add_css_class('caption')
        box.append(ts)

        self.set_child(box)

        # Full title tooltip
        full_title = ref.title if ref.title else '(untitled)'
        try:
            dt = datetime.fromtimestamp(ref.last_active / 1000)
            tooltip = f'{full_title}\n{dt.strftime("%Y-%m-%d %H:%M")}'
        except (ValueError, OSError):
            tooltip = full_title
        self.set_tooltip_text(tooltip)


class ResourceBar(Gtk.Box):
    def __init__(self, on_settings_clicked=None, version=''):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class('resource-bar')

        self._reader = ResourceReader()

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self._cpu_label = Gtk.Label(label='CPU: \u2014')
        self._cpu_label.set_halign(Gtk.Align.START)
        self._cpu_label.add_css_class('caption')
        top.append(self._cpu_label)

        self._ram_label = Gtk.Label(label='RAM: \u2014')
        self._ram_label.set_halign(Gtk.Align.START)
        self._ram_label.add_css_class('caption')
        self._ram_label.set_hexpand(True)
        top.append(self._ram_label)

        gear = Gtk.Button.new_from_icon_name('emblem-system-symbolic')
        gear.add_css_class('flat')
        gear.add_css_class('circular')
        gear.set_valign(Gtk.Align.CENTER)
        gear.set_tooltip_text('Settings')
        if on_settings_clicked is not None:
            gear.connect('clicked', lambda b: on_settings_clicked())
        top.append(gear)

        self.append(top)

        if version:
            ver_label = Gtk.Label(label=f'ProjectMan v{version}')
            ver_label.set_halign(Gtk.Align.START)
            ver_label.add_css_class('pm-version-label')
            self.append(ver_label)

    def start_polling(self):
        self._reader.read()
        GLib.timeout_add(3000, self._update)

    def _update(self):
        data = self._reader.read()
        self._cpu_label.set_label(f"CPU: {data['cpu_pct']:.0f}%")
        mem = data['mem_mb']
        if mem >= 1024:
            self._ram_label.set_label(f"RAM: {mem / 1024:.1f} GB")
        else:
            self._ram_label.set_label(f"RAM: {mem:.0f} MB")
        return GLib.SOURCE_CONTINUE
