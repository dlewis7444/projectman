from datetime import datetime

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GLib, GObject, Gdk, Pango

import harnesses
from model import ResourceReader
from models import FOLLOW_DEFAULT, NATIVE_LABEL


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
        # host_id, project name
        'project-create':       (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'show-paa-window':      (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-ai-scan':      (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-model-change': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'project-harness-change': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # host_id, expanded
        'host-section-toggled': (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, store, history, watcher, version='', settings=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class('pm-sidebar')
        self._store = store
        # ``history`` retained only for back-compat construction; the expander
        # now pulls sessions through the per-project adapter (A1), not the
        # HistoryReader directly. ``settings`` lets each row resolve its
        # effective harness so the seam is load-bearing.
        self._history = history
        self._settings = settings
        self._watcher = watcher
        self._rows = {}
        self._sections = {}         # host_id → HostSection
        self._section_headers = {}  # host_id → HostSectionHeader (in-flow)
        self._row_host = {}         # path → host_id
        self._new_project_row = None
        self._new_project_host_id = 'localhost'
        self._filter_text = ''
        self._sticky_host_id = None
        # Per-project model menu state, pushed in via set_model_options().
        self._model_options = []
        self._model_overrides = {}
        self._global_model_label = NATIVE_LABEL
        # Remote project lists keyed by host_id (Phase 2 fills these).
        self._remote_projects = {}  # host_id → list[Project]
        self._host_health = {}      # host_id → 'grey'|'green'|'yellow'|'red'
        # Durable process state — survives process-started BEFORE the row exists
        # (remote restore spawns SSH, then async list builds rows later).
        # path → (state, is_zellij|None)
        self._process_states = {}
        # path → harness_id | None (C5 running harness)
        self._running_harnesses = {}
        # Deferred deactivate grace: path stays 'attached' until timer fires.
        self._pending_deactivates = set()
        self._pending_deactivate_timers = {}  # path → GLib timeout id
        self.PENDING_DEACTIVATE_MS = 5000

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(6)
        btn_row.set_margin_end(8)
        btn_row.set_margin_bottom(2)

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

        # Overlay: scrollable per-host sections + sticky host header pin.
        self._overlay = Gtk.Overlay()
        self._overlay.set_vexpand(True)
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._scrolled.set_child(self._sections_box)
        self._overlay.set_child(self._scrolled)

        # Sticky clone: same HostSectionHeader chrome, pinned at viewport top.
        self._sticky_header = HostSectionHeader(
            host_id='localhost',
            title='localhost',
            expanded=True,
            show_health=False,
            on_toggle=self._on_section_toggle,
            on_add_project=self._on_section_add_project,
        )
        self._sticky_header.add_css_class('pm-sticky-host')
        self._sticky_header.set_halign(Gtk.Align.FILL)
        self._sticky_header.set_valign(Gtk.Align.START)
        self._sticky_header.set_hexpand(True)
        self._sticky_header.set_visible(False)
        self._overlay.add_overlay(self._sticky_header)

        adj = self._scrolled.get_vadjustment()
        adj.connect('value-changed', self._on_scroll_changed)
        # Recompute pin after layout settles (filters, rebuilds, resize).
        self._sections_box.connect(
            'map', lambda *a: GLib.idle_add(self._update_sticky_header_idle))
        self.append(self._overlay)

        self._count_label = Gtk.Label()
        self._count_label.add_css_class('dim-label')
        self._count_label.add_css_class('caption')
        self._count_label.set_margin_top(4)
        self._count_label.set_margin_bottom(0)
        self.append(self._count_label)

        # Active Only toggle removed: click a host header to cycle
        # hide all → active only → show all (per section).

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

    def _filter_func_for(self, host_id):
        """Per-host ListBox filter: section mode + name filter."""
        def _filter_row(row):
            if isinstance(row, NewProjectEntryRow):
                return self._section_mode(host_id) != 'hidden'
            if isinstance(row, ProjectRow):
                mode = self._section_mode(host_id)
                if mode == 'hidden':
                    return False
                if mode == 'active' and row._process_state not in ('attached', 'detached'):
                    return False
                if self._filter_text and self._filter_text not in row._project.name.lower():
                    return False
            return True
        return _filter_row

    def _section_mode(self, host_id):
        if self._settings is not None:
            return self._settings.section_mode(host_id)
        return 'all'

    def _is_section_expanded(self, host_id):
        return self._section_mode(host_id) != 'hidden'

    def _invalidate_filters(self):
        for section in self._sections.values():
            section.listbox.invalidate_filter()
        GLib.idle_add(self._update_sticky_header_idle)

    def _update_sticky_header_idle(self):
        self._update_sticky_header()
        return False

    def set_filter_text(self, text):
        self._filter_text = text.lower()
        self._invalidate_filters()

    def _host_order(self):
        """localhost first, then remotes in stable settings order."""
        from hosts import LOCALHOST_ID
        order = [LOCALHOST_ID]
        if self._settings is not None:
            for hid in self._settings.host_profiles():
                if hid not in order:
                    order.append(hid)
        return order

    def _projects_for_host(self, host_id):
        from hosts import LOCALHOST_ID
        if host_id == LOCALHOST_ID:
            return list(self._store.load_projects())
        return list(self._remote_projects.get(host_id, []))

    def _append_project_row(self, proj, running_state, running_agent):
        row = ProjectRow(proj, self._history, self._watcher,
                         settings=self._settings)
        # Prefer durable maps (survive spawn-before-row), then row snapshot.
        if proj.path in self._process_states:
            st, zj = self._process_states[proj.path]
            row.set_process_state(st, is_zellij=zj)
        elif proj.path in running_state:
            row._process_state = running_state[proj.path]
            row.update_status()
        if proj.path in self._running_harnesses:
            row.set_running_harness(self._running_harnesses[proj.path])
        elif running_agent.get(proj.path) is not None:
            row.set_running_harness(running_agent[proj.path])
        row.connect('session-activated',
                    lambda r, p, sid, pp=proj.path: self.emit('session-activated', pp, sid))
        # Archive only for localhost projects
        from hosts import LOCALHOST_ID
        if getattr(proj, 'host_id', LOCALHOST_ID) == LOCALHOST_ID:
            row.connect('project-archive',
                        lambda r, p=proj.path: self.emit('project-archive', p))
        row.connect('deactivate-requested',
                    lambda r, p=proj.path: self._begin_pending_deactivate(p))
        row.connect('deactivate-undo',
                    lambda r, p=proj.path: self.cancel_pending_deactivate(p))
        if proj.path in self._pending_deactivates:
            row.set_pending_deactivate(True)
        row.connect('project-new-session',
                    lambda r, p=proj.path: self.emit('project-new-session', p))
        row.connect('project-zellij',
                    lambda r, p=proj.path: self.emit('project-zellij', p))
        row.connect('project-ntfy-toggle',
                    lambda r, p=proj.path: self.emit('project-ntfy-toggle', p))
        row.connect('project-ai-scan',
                    lambda r, p=proj.path: self.emit('project-ai-scan', p))
        row.connect('project-rename',
                    lambda r, new_name, p=proj.path: self.emit('project-rename', p, new_name))
        row.connect('project-model-change',
                    lambda r, mid, p=proj.path: self.emit('project-model-change', p, mid))
        row.connect('project-harness-change',
                    lambda r, aid, p=proj.path: self.emit('project-harness-change', p, aid))
        row.set_model_options(
            self._model_options,
            self._model_overrides.get(proj.path, FOLLOW_DEFAULT),
            self._global_model_label,
        )
        host_id = getattr(proj, 'host_id', 'localhost')
        section = self._sections.get(host_id)
        if section is not None:
            section.listbox.append(row)
        self._rows[proj.path] = row
        self._row_host[proj.path] = host_id

    def _clear_sections(self):
        child = self._sections_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._sections_box.remove(child)
            child = nxt
        self._sections.clear()
        self._section_headers.clear()
        self._sticky_host_id = None
        self._sticky_header.set_visible(False)

    def _populate(self):
        from hosts import LOCALHOST_ID
        # Preserve the in-progress new-project entry across rebuilds
        pending_row = self._new_project_row
        pending_host = self._new_project_host_id
        if pending_row is not None:
            parent = pending_row.get_parent()
            if parent is not None:
                parent.remove(pending_row)
            self._new_project_row = None
        # Snapshot row state (also mirrored into durable maps by setters).
        running_state = {path: row._process_state for path, row in self._rows.items()}
        running_agent = {path: row._running_harness for path, row in self._rows.items()}

        self._rows.clear()
        self._row_host.clear()
        self._clear_sections()

        for host_id in self._host_order():
            title = 'localhost'
            show_health = False
            if host_id != LOCALHOST_ID and self._settings is not None:
                prof = self._settings.host_profiles().get(host_id)
                title = prof.title() if prof is not None else host_id
                show_health = True
            mode = self._section_mode(host_id)
            header = HostSectionHeader(
                host_id=host_id,
                title=title,
                expanded=mode != 'hidden',
                show_health=show_health,
                on_toggle=self._on_section_toggle,
                on_add_project=self._on_section_add_project,
            )
            header.set_filter_mode(mode)
            health = self._host_health.get(host_id, 'grey')
            header.set_health(health)

            listbox = Gtk.ListBox()
            listbox.add_css_class('navigation-sidebar')
            listbox.connect('row-activated', self._on_row_activated)
            listbox.set_filter_func(self._filter_func_for(host_id))

            section = HostSection(host_id, header, listbox)
            self._sections_box.append(section)
            self._sections[host_id] = section
            self._section_headers[host_id] = header

            projects = self._projects_for_host(host_id)
            for proj in projects:
                if not getattr(proj, 'host_id', None):
                    proj.host_id = host_id
                self._append_project_row(proj, running_state, running_agent)

            # Pending new-project row sits under its host section
            if pending_row is not None and pending_host == host_id:
                self._new_project_row = pending_row
                self._new_project_host_id = pending_host
                listbox.append(pending_row)
                GLib.idle_add(lambda: pending_row._entry.grab_focus() and False)

        self._refresh_section_counts()
        self._update_count_label()
        self._invalidate_filters()
        GLib.idle_add(self._update_sticky_header_idle)

    def _host_id_for_path(self, path):
        if path in self._row_host:
            return self._row_host[path]
        from hosts import decode_project_ref
        host_id, _ = decode_project_ref(path)
        return host_id

    def set_host_section_mode(self, host_id, mode):
        """Set filter mode for one host (without cycling)."""
        if mode not in ('hidden', 'active', 'all'):
            mode = 'all'
        if self._settings is not None:
            self._settings.set_section_mode(host_id, mode)
        header = self._section_headers.get(host_id)
        if header is not None:
            header.set_expanded(mode != 'hidden')
            header.set_filter_mode(mode)
        if self._sticky_host_id == host_id:
            self._sticky_header.set_expanded(mode != 'hidden')
            self._sticky_header.set_filter_mode(mode)
        self.emit('host-section-toggled', host_id, mode != 'hidden')
        self._invalidate_filters()
        self._update_count_label()

    def _on_section_toggle(self, host_id):
        """Cycle section filter: hidden → active → all → hidden."""
        order = ('hidden', 'active', 'all')
        cur = self._section_mode(host_id)
        try:
            nxt = order[(order.index(cur) + 1) % len(order)]
        except ValueError:
            nxt = 'all'
        self.set_host_section_mode(host_id, nxt)

    def _on_section_add_project(self, host_id):
        # Creating requires the section not fully hidden.
        if self._section_mode(host_id) == 'hidden':
            self.set_host_section_mode(host_id, 'all')
        self._on_add_project(None, host_id=host_id)

    def _on_row_activated(self, listbox, row):
        if isinstance(row, ProjectRow):
            self.emit('project-activated', row._project.path)

    def _refresh_section_counts(self):
        """Update each section header's active(total) counts."""
        from collections import defaultdict
        totals = defaultdict(int)
        actives = defaultdict(int)
        for path, row in self._rows.items():
            hid = self._row_host.get(path, 'localhost')
            totals[hid] += 1
            if row._process_state in ('attached', 'detached'):
                actives[hid] += 1
        # Remote hosts with zero cached projects still need 0(0)
        for host_id, header in self._section_headers.items():
            a, t = actives.get(host_id, 0), totals.get(host_id, 0)
            header.set_counts(a, t)
            if self._sticky_host_id == host_id:
                self._sticky_header.set_counts(a, t)

    def _update_count_label(self):
        active_n = sum(1 for row in self._rows.values()
                       if row._process_state in ('attached', 'detached'))
        total_n = len(self._rows)
        self._count_label.set_label(f'{active_n} active · {total_n} projects')

    def refresh_status(self):
        for row in self._rows.values():
            row.update_status()

    def refresh(self):
        self._populate()

    def set_active_only(self, active, path=None, paths=None):
        """Per-host Active Only: maps to section mode 'active' / 'all'.

        path — single project (spawn success/failure on that host).
        paths — iterable of project paths (restore); each host → 'active'.
        Neither — all known hosts (legacy global call sites).
        """
        if path is not None:
            host_id = self._host_id_for_path(path)
            if active:
                self.set_host_section_mode(host_id, 'active')
            elif self._section_mode(host_id) == 'active':
                self.set_host_section_mode(host_id, 'all')
            return

        if paths is not None:
            if not active:
                return
            host_ids = {self._host_id_for_path(p) for p in paths}
            for host_id in host_ids:
                self.set_host_section_mode(host_id, 'active')
            return

        if active:
            for host_id in self._section_headers:
                self.set_host_section_mode(host_id, 'active')
        else:
            for host_id in self._section_headers:
                if self._section_mode(host_id) == 'active':
                    self.set_host_section_mode(host_id, 'all')

    def select_project(self, path):
        row = self._rows.get(path)
        if row is None:
            return
        host_id = self._row_host.get(path, 'localhost')
        for hid, section in self._sections.items():
            if hid == host_id:
                section.listbox.select_row(row)
            else:
                section.listbox.unselect_all()

    def set_project_state(self, path, state: str, is_zellij: bool = None):
        """Record process liveness for *path* even if the row is not built yet.

        Remote restore spawns SSH before the async project list creates the
        ProjectRow; without a durable map those starts were invisible to the
        sidebar (instant reattach on click, grey dots, wrong active counts).
        """
        self._process_states[path] = (state, is_zellij)
        if state == 'inactive':
            self.cancel_pending_deactivate(path)
        if path in self._rows:
            self._rows[path].set_process_state(state, is_zellij=is_zellij)
        # Active-only sections need a refilter when process state changes.
        self._invalidate_filters()
        self._refresh_section_counts()
        self._update_count_label()

    def _cancel_pending_deactivate_timer(self, path):
        tid = self._pending_deactivate_timers.pop(path, None)
        if tid is not None:
            GLib.source_remove(tid)

    def cancel_pending_deactivate(self, path):
        """Cancel a pending deactivate (UNDO, natural exit, or immediate-kill paths)."""
        if path not in self._pending_deactivates:
            return
        self._pending_deactivates.discard(path)
        self._cancel_pending_deactivate_timer(path)
        if path in self._rows:
            self._rows[path].set_pending_deactivate(False)

    def cancel_all_pending_deactivates(self):
        """Clear every pending deactivate without killing sessions."""
        for path in list(self._pending_deactivates):
            self.cancel_pending_deactivate(path)

    def migrate_pending_deactivate(self, old_path, new_path):
        """Rewrite pending-deactivate keys when a project path changes (rename)."""
        if old_path not in self._pending_deactivates:
            return
        self._pending_deactivates.discard(old_path)
        self._pending_deactivates.add(new_path)
        self._cancel_pending_deactivate_timer(old_path)
        self._pending_deactivate_timers[new_path] = GLib.timeout_add(
            self.PENDING_DEACTIVATE_MS, self._fire_pending_deactivate, new_path)

    def _begin_pending_deactivate(self, path):
        """Start the hidden grace period; kill only when the timer fires."""
        if path in self._pending_deactivates:
            return
        row = self._rows.get(path)
        if row is None or row._process_state != 'attached':
            return
        self._pending_deactivates.add(path)
        row.set_pending_deactivate(True)
        self._cancel_pending_deactivate_timer(path)
        self._pending_deactivate_timers[path] = GLib.timeout_add(
            self.PENDING_DEACTIVATE_MS, self._fire_pending_deactivate, path)

    def _fire_pending_deactivate(self, path):
        self._pending_deactivate_timers.pop(path, None)
        if path not in self._pending_deactivates:
            return False
        self._pending_deactivates.discard(path)
        if path in self._rows:
            self._rows[path].set_pending_deactivate(False)
        self.emit('project-deactivate', path)
        return False

    def set_remote_projects(self, host_id, projects):
        """Replace the cached project list for a remote host.

        Rebuilds the sidebar only when the name set changes — health polls
        must not thrash the whole list every 30s (UI freeze).
        """
        new = list(projects or [])
        old = self._remote_projects.get(host_id, [])
        self._remote_projects[host_id] = new
        old_names = sorted(p.name for p in old)
        new_names = sorted(p.name for p in new)
        if old_names != new_names:
            self._populate()
        else:
            # Counts may still change via process state; refresh header totals.
            self._refresh_section_counts()

    def set_host_health(self, host_id, state: str):
        """Update micro health indicator on a remote section header."""
        self._host_health[host_id] = state
        header = self._section_headers.get(host_id)
        if header is not None:
            header.set_health(state)
        if self._sticky_host_id == host_id:
            self._sticky_header.set_health(state)

    def set_running_harness(self, path, harness_id):
        """Push the live child's actual agent onto a row (C5). ``None`` clears
        it. Durable even when the row does not exist yet."""
        self._running_harnesses[path] = harness_id
        if path in self._rows:
            self._rows[path].set_running_harness(harness_id)

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
        """Push updated settings so rows re-resolve their effective harness.

        Called from window.apply_settings after a harness/model override change
        so the per-row caps gating (A5) and session source (A1) follow the new
        effective harness.
        """
        self._settings = settings
        for row in self._rows.values():
            row.set_settings(settings)

    def set_model_options(self, options, overrides, global_label):
        """Push provider menu options to every project row.

        Each row rebuilds its Provider submenu from settings (natives + customs,
        selectable by effective harness). ``options`` / ``overrides`` /
        ``global_label`` are kept for call-compat; rows prefer live settings.
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

    def _on_add_project(self, button, host_id='localhost'):
        if self._new_project_row is not None:
            self._new_project_row._entry.grab_focus()
            return
        # Ensure target section is expanded so the entry row is visible.
        if not self._is_section_expanded(host_id):
            if self._settings is not None:
                self._settings.set_section_expanded(host_id, True)
            header = self._section_headers.get(host_id)
            if header is not None:
                header.set_expanded(True)
            if self._sticky_host_id == host_id:
                self._sticky_header.set_expanded(True)
            self.emit('host-section-toggled', host_id, True)
        row = NewProjectEntryRow(
            on_commit=self._commit_new_project,
            on_cancel=self._cancel_new_project,
        )
        self._new_project_row = row
        self._new_project_host_id = host_id
        section = self._sections.get(host_id)
        if section is not None:
            section.listbox.prepend(row)
            section.listbox.invalidate_filter()
        GLib.idle_add(lambda: row._entry.grab_focus() and False)
        GLib.idle_add(self._update_sticky_header_idle)

    def _commit_new_project(self, name):
        row = self._new_project_row
        host_id = self._new_project_host_id
        self._new_project_row = None
        self._new_project_host_id = 'localhost'
        if row is not None:
            parent = row.get_parent()
            if parent is not None:
                parent.remove(row)
        self.emit('project-create', host_id, name)

    def _cancel_new_project(self):
        if self._new_project_row is None:
            return
        parent = self._new_project_row.get_parent()
        if parent is not None:
            parent.remove(self._new_project_row)
        self._new_project_row = None
        self._new_project_host_id = 'localhost'

    def _on_scroll_changed(self, *_args):
        self._update_sticky_header()

    def _widget_y_in_content(self, widget):
        """Y of *widget* top edge relative to the sections content box."""
        if widget is None or not widget.get_mapped():
            return None
        try:
            result = widget.translate_coordinates(self._sections_box, 0, 0)
        except Exception:
            return None
        if result is None:
            return None
        # PyGObject may return (ok, x, y) or (x, y)
        if len(result) == 3:
            ok, _x, y = result
            return float(y) if ok else None
        if len(result) == 2:
            _x, y = result
            return float(y)
        return None

    def _update_sticky_header(self):
        """Pin the current host header at the top of the scroll viewport.

        Classic section-sticky: header stays put while its projects scroll
        underneath; the next host header pushes it off.
        """
        adj = self._scrolled.get_vadjustment()
        scroll_y = float(adj.get_value())
        order = [hid for hid in self._host_order() if hid in self._sections]
        if not order:
            self._sticky_header.set_visible(False)
            self._sticky_host_id = None
            return

        sticky_host = None
        next_header_y = None
        for i, host_id in enumerate(order):
            section = self._sections[host_id]
            y = self._widget_y_in_content(section.header)
            if y is None:
                continue
            section_h = float(section.get_allocated_height() or 0)
            section_bottom = y + section_h
            # Past this section's header top, still inside the section.
            if y <= scroll_y < section_bottom:
                sticky_host = host_id
                if i + 1 < len(order):
                    next_header_y = self._widget_y_in_content(
                        self._sections[order[i + 1]].header)
                break

        # Don't pin while the in-flow header is still fully visible.
        if sticky_host is not None:
            header_y = self._widget_y_in_content(self._sections[sticky_host].header)
            if header_y is not None and header_y >= scroll_y - 0.5:
                sticky_host = None

        if sticky_host is None:
            self._sticky_header.set_visible(False)
            self._sticky_header.set_margin_top(0)
            self._sticky_host_id = None
            return

        src = self._section_headers.get(sticky_host)
        if src is None:
            self._sticky_header.set_visible(False)
            self._sticky_host_id = None
            return

        if self._sticky_host_id != sticky_host:
            self._sticky_host_id = sticky_host
            self._sticky_header.bind_from(src)

        self._sticky_header.set_visible(True)

        # Push sticky up as the next host header approaches (Excel freeze feel).
        sticky_h = float(self._sticky_header.get_allocated_height() or 0)
        if sticky_h <= 0:
            sticky_h = float(src.get_allocated_height() or 0)
        push = 0.0
        if next_header_y is not None and sticky_h > 0:
            dist = next_header_y - scroll_y
            if dist < sticky_h:
                push = sticky_h - max(dist, 0.0)
        self._sticky_header.set_margin_top(int(-push))


class HostSection(Gtk.Box):
    """One host block: sticky-able header + project ListBox."""

    def __init__(self, host_id, header, listbox):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.host_id = host_id
        self.header = header
        self.listbox = listbox
        self.add_css_class('pm-host-section-block')
        self.append(header)
        self.append(listbox)


class HostSectionHeader(Gtk.Box):
    """Section header for a host (localhost or remote).

    Click cycles filter mode: hide all → active only → show all.
    Shows optional health micro-dot and ``active(total)`` counts.
    Not a ListBoxRow — lives above each host's project list (and as the
    sticky overlay clone).
    """

    _MODE_TIPS = {
        'hidden': 'Hidden — click for active only',
        'active': 'Active only — click for show all',
        'all': 'Show all — click to hide',
    }
    _MODE_LABELS = {
        'all': '(all projects)',
        'active': '(active projects)',
        'hidden': '(projects hidden)',
    }

    def __init__(self, host_id, title, expanded, show_health, on_toggle, on_add_project):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.host_id = host_id
        self._expanded = expanded
        self._filter_mode = 'all' if expanded else 'hidden'
        self._title_text = title
        self._show_health = show_health
        self._on_toggle = on_toggle
        self._on_add_project = on_add_project
        self.add_css_class('pm-host-section')
        self.set_hexpand(True)

        self._health_dot = Gtk.Box()
        self._health_dot.add_css_class('host-health-dot')
        self._health_dot.set_valign(Gtk.Align.CENTER)
        self._health_dot.set_margin_start(8)
        self._health_dot.set_visible(show_health)
        self.append(self._health_dot)

        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_col.set_hexpand(True)
        title_col.set_margin_top(8)
        title_col.set_margin_bottom(6)
        self._title_label = Gtk.Label(label=title, xalign=0)
        self._title_label.add_css_class('pm-host-section-title')
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_label.set_hexpand(True)
        title_col.append(self._title_label)
        # Filter mode under the name: (all projects) / (active projects) / (projects hidden)
        self._filter_label = Gtk.Label(label=self._MODE_LABELS['all'], xalign=0)
        self._filter_label.add_css_class('dim-label')
        self._filter_label.add_css_class('pm-host-section-filter')
        self._filter_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_col.append(self._filter_label)
        self.append(title_col)

        self._count_label = Gtk.Label(label='0(0)')
        self._count_label.add_css_class('dim-label')
        self._count_label.add_css_class('caption')
        self._count_label.add_css_class('pm-host-section-count')
        self._count_label.set_valign(Gtk.Align.CENTER)
        self.append(self._count_label)

        self._add_btn = Gtk.Button.new_from_icon_name('list-add-symbolic')
        self._add_btn.add_css_class('flat')
        self._add_btn.add_css_class('circular')
        self._add_btn.set_tooltip_text('New Project')
        self._add_btn.set_has_frame(False)
        self._add_btn.set_valign(Gtk.Align.CENTER)
        self._add_btn.set_margin_end(4)
        self._add_btn.connect('clicked', self._on_add_clicked)
        self.append(self._add_btn)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect('released', self._on_header_released)
        self.add_controller(click)

        self.set_expanded(expanded)
        self.set_filter_mode(self._filter_mode)
        if show_health:
            self.set_health('grey')

    def _on_add_clicked(self, button):
        self._on_add_project(self.host_id)

    def _on_header_released(self, gesture, n_press, x, y):
        if n_press < 1:
            return
        # Ignore clicks on the add button (it handles its own action).
        if self._add_btn.contains(x, y) if hasattr(self._add_btn, 'contains') else False:
            return
        # Fallback: if event is over add_btn allocation.
        ok = self._add_btn.translate_coordinates(self, 0, 0)
        if ok is not None:
            if len(ok) == 3:
                success, bx, by = ok
                if success:
                    alloc = self._add_btn.get_allocation()
                    if bx <= x <= bx + alloc.width and by <= y <= by + alloc.height:
                        return
            elif len(ok) == 2:
                bx, by = ok
                alloc = self._add_btn.get_allocation()
                if bx <= x <= bx + alloc.width and by <= y <= by + alloc.height:
                    return
        if self._on_toggle is not None:
            self._on_toggle(self.host_id)

    def set_title(self, title: str):
        self._title_text = title
        self._title_label.set_label(title)
        tip = self._MODE_TIPS.get(self._filter_mode, '')
        self._title_label.set_tooltip_text(f'{self._title_text}\n{tip}')

    def set_expanded(self, expanded):
        self._expanded = bool(expanded)

    def set_filter_mode(self, mode: str):
        if mode not in ('hidden', 'active', 'all'):
            mode = 'all'
        self._filter_mode = mode
        if hasattr(self, '_filter_label'):
            self._filter_label.set_label(self._MODE_LABELS.get(mode, self._MODE_LABELS['all']))
        tip = self._MODE_TIPS.get(mode, '')
        self._title_label.set_tooltip_text(f'{self._title_text}\n{tip}')
        self.set_tooltip_text(tip)

    def set_counts(self, active: int, total: int):
        self._count_label.set_label(f'{int(active)}({int(total)})')

    def set_health(self, state: str):
        self._health_dot.set_visible(self._show_health)
        for s in ('grey', 'green', 'yellow', 'red'):
            self._health_dot.remove_css_class(f'host-health-{s}')
        state = state if state in ('grey', 'green', 'yellow', 'red') else 'grey'
        self._health_dot.add_css_class(f'host-health-{state}')
        tips = {
            'grey': 'Health check off or not yet run',
            'green': 'Reachable',
            'yellow': 'Reachable with problems',
            'red': 'Unreachable',
        }
        self._health_dot.set_tooltip_text(tips.get(state, ''))

    def bind_from(self, other: 'HostSectionHeader'):
        """Copy identity + chrome from another header (sticky pin sync)."""
        self.host_id = other.host_id
        self._show_health = other._show_health
        self.set_title(other._title_text)
        self.set_filter_mode(other._filter_mode)
        self.set_expanded(other._expanded)
        self._count_label.set_label(other._count_label.get_label())
        self._health_dot.set_visible(other._health_dot.get_visible())
        for s in ('grey', 'green', 'yellow', 'red'):
            self._health_dot.remove_css_class(f'host-health-{s}')
            if other._health_dot.has_css_class(f'host-health-{s}'):
                self._health_dot.add_css_class(f'host-health-{s}')
                self._health_dot.set_tooltip_text(
                    other._health_dot.get_tooltip_text() or '')


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
        'deactivate-requested': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'deactivate-undo':      (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-new-session': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-zellij':     (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-ntfy-toggle': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-ai-scan':    (GObject.SignalFlags.RUN_FIRST, None, ()),
        'project-rename':     (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-model-change': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'project-harness-change': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
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
        self._pending_deactivate = False
        self._is_zellij = False
        self._new_session_row = None
        # The harness id the LIVE child is actually running (C5 truth), pushed in
        # from window.py's process-started/exited flow. None when nothing runs.
        # When it disagrees with the configured/effective harness (a restored
        # saved-harness-wins session, A2), the subtitle leads with what's running.
        self._running_harness = None

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

        # Name + an effective-harness subtitle (B3). Vertical so the subtitle
        # sits under the name; the box carries the hexpand the name used to.
        self._name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._name_box.set_hexpand(True)
        self._name_box.set_valign(Gtk.Align.CENTER)
        self._name_label = Gtk.Label(label=project.name)
        self._name_label.set_halign(Gtk.Align.START)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_box.append(self._name_label)
        self._subtitle_label = Gtk.Label()
        self._subtitle_label.set_halign(Gtk.Align.START)
        self._subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._subtitle_label.add_css_class('dim-label')
        self._subtitle_label.add_css_class('caption')
        self._subtitle_label.add_css_class('pm-harness-subtitle')
        self._subtitle_label.set_visible(False)
        self._name_box.append(self._subtitle_label)
        top.append(self._name_box)

        self._rename_entry = Gtk.Entry()
        self._rename_entry.set_hexpand(True)
        self._rename_entry.set_visible(False)
        self._rename_entry.connect('activate', self._on_rename_activate)
        rename_key = Gtk.EventControllerKey.new()
        rename_key.connect('key-pressed', self._on_rename_key)
        self._rename_entry.add_controller(rename_key)
        # Focus-leave cancels rename, but the context-menu popover close
        # steals/restores focus and would cancel immediately. Arm leave
        # handling only after rename focus has settled (see _enter_rename_mode).
        self._rename_active = False
        self._rename_ignore_leave = True
        rename_focus = Gtk.EventControllerFocus.new()
        rename_focus.connect('leave', self._on_rename_focus_leave)
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
        self._deactivate_btn.connect('clicked',
                                     lambda b: self.emit('deactivate-requested'))
        actions_box.append(self._deactivate_btn)

        self._undo_btn = Gtk.Button(label='UNDO')
        self._undo_btn.add_css_class('flat')
        self._undo_btn.set_valign(Gtk.Align.CENTER)
        self._undo_btn.set_tooltip_text('Cancel deactivate')
        self._undo_btn.set_visible(False)
        self._undo_btn.connect('clicked', lambda b: self.emit('deactivate-undo'))
        actions_box.append(self._undo_btn)

        self._actions_box = actions_box
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

    # --- harness seam (A1 sessions, A5 caps gating) -------------------------

    def _adapter(self):
        """The effective harness adapter for this project (claude if unknown).

        Resolved through the seam so the expander, the session-resume action,
        and the Provider submenu all degrade on the adapter's declared caps rather
        than assuming claude. Falls back to the claude adapter when settings
        weren't injected (defensive — keeps headless/legacy construction sane).
        """
        harness_id = (self._settings.effective_harness(self._project.path)
                    if self._settings is not None else 'claude')
        # F9: thread settings into get_adapter so a named-but-missing harness's
        # caps-gating reflects the M-P3.2 fallback (harness_default → first
        # available), not a hardcoded claude. With settings the row gates on the
        # adapter that will ACTUALLY run for this project.
        return harnesses.get_adapter(harness_id, self._settings)

    def _caps(self):
        return self._adapter().caps

    def _remap_idle_to_done(self):
        """Whether an attached row's watcher-'idle' should render as 'done'.

        Gated on the effective adapter's ``caps.rich_status`` (its first
        consumer): a rich-status harness (claude, opencode — both bridged) with
        no status file yet is a genuinely just-started/finished state, so the
        historic green remap stands. A ``rich_status=False`` agent has no
        bridge at all — remapping would pin a fake green "work finished" dot
        for the entire run; it keeps the honest dim 'idle' instead.

        Remote projects never get the fake-green remap: status must come from
        polled remote status files (host rich-status opt-in). Without a hit,
        stay dim 'idle' so the dot is not stuck green for the whole session.
        """
        try:
            from hosts import LOCALHOST_ID
            if getattr(self._project, 'host_id', LOCALHOST_ID) != LOCALHOST_ID:
                return False
            return self._adapter().caps.rich_status
        except Exception:
            return True

    def set_settings(self, settings):
        """Re-bind settings and re-apply caps gating (effective harness changed)."""
        self._settings = settings
        self._populate_harness_submenu()
        self._apply_caps()
        self._rebuild_popover()

    def _apply_caps(self):
        """Show/hide caps-gated menu entries for the effective adapter (A5).

        * Provider submenu     — caps.model_select
        * History expander  — caps.sessions (the expand arrow + new-session row)
        * Session resume     — caps.resume_by_id (gated where the row is built)

        Idempotent: safe to call after construction and on every settings push.
        """
        caps = self._caps()
        # Provider submenu visibility.
        self._set_menu_item_present('Provider', caps.model_select,
                                    self._insert_model_submenu)
        # History expander: hide the arrow entirely when the harness can't
        # enumerate sessions, so there's no empty dropdown to open.
        if hasattr(self, '_arrow'):
            self._arrow.set_visible(caps.sessions)
        self._update_subtitle()

    def _subtitle_text(self):
        """Pure builder for the row subtitle string (B3 + C5). Returns the text
        to show, or ``None`` when the row should stay clean (no subtitle).

        Two shapes:
          * NO live mismatch — the running harness is absent OR equals the
            configured/effective harness: today's string, BYTE-IDENTICAL —
            ``<AgentDisplay>`` (+ ``" · " + model`` when a model is pinned),
            and ``None`` for a plain default harness with no model.
          * LIVE mismatch — a child is running a DIFFERENT agent than the one
            configured for the next session (a restored saved-harness-wins
            session, A2): lead with what is ACTUALLY running, naming the next:
            ``<RunningDisplay> (next: <ConfiguredDisplay>)`` (+ the model
            suffix, preserved in both shapes). Always shown — the truth of a
            live mismatch overrides the clean-default hide rule (C5).
        """
        if self._settings is None:
            return None
        harness_id = self._settings.effective_harness(self._project.path)
        model = self._settings.effective_model(self._project.path)
        running = self._running_harness
        mismatch = running is not None and running != harness_id
        if mismatch:
            head = (f'{self._harness_display_name(running)} '
                    f'(next: {self._harness_display_name(harness_id)})')
        else:
            is_default_agent = (
                harness_id == self._settings.harness_default == harnesses.DEFAULT_HARNESS)
            if is_default_agent and not model:
                return None
            head = self._harness_display_name(harness_id)
        parts = [head]
        if model:
            parts.append(model)
        return ' · '.join(parts)

    def _update_subtitle(self):
        """Render the subtitle from the pure builder (B3 + C5).

        Surfaces a non-default configuration — or a live harness mismatch — without
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

    def set_running_harness(self, harness_id):
        """Record the harness the live child is actually running (C5) and refresh
        the subtitle. ``None`` clears it (the session ended)."""
        self._running_harness = harness_id
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
        """Re-attach the Provider submenu (used by caps gating to restore it).

        Appended at the end on re-add (the initial build placed it mid-menu);
        the position only shifts on a low→high caps transition, which is rare
        and cosmetic.
        """
        self._menu.append_submenu('Provider', self._model_submenu)

    def _setup_context_menu(self):
        self._menu = Gio.Menu()
        self._menu.append('New Session',        'row.new-session')
        self._menu.append('New Zellij Session', 'row.zellij')
        self._menu.append('AI Scan',            'row.ai-scan')
        self._harness_submenu = Gio.Menu()
        self._menu.append_submenu('Harness', self._harness_submenu)
        self._model_submenu = Gio.Menu()
        self._menu.append_submenu('Provider', self._model_submenu)
        self._menu.append('Rename',             'row.rename')
        from hosts import LOCALHOST_ID as _LH
        if getattr(self._project, 'host_id', _LH) == _LH:
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
        _add('ai-scan',      'project-ai-scan')
        if getattr(self._project, 'host_id', _LH) == _LH:
            _add('archive',      'project-archive')

        rename_action = Gio.SimpleAction.new('rename', None)
        rename_action.connect('activate', lambda a, p: self._enter_rename_mode())
        ag.add_action(rename_action)

        self._ntfy_action = Gio.SimpleAction.new_stateful(
            'ntfy-done', None, GLib.Variant('b', False)
        )
        self._ntfy_action.connect('activate', self._on_ntfy_activate)
        ag.add_action(self._ntfy_action)

        # Per-project model selection. A single stateful 's' action lets GTK
        # render the submenu as radio items with the active one checked.
        self._model_action = Gio.SimpleAction.new_stateful(
            'set-model', GLib.VariantType.new('s'),
            GLib.Variant('s', FOLLOW_DEFAULT),
        )
        self._model_action.connect('activate', self._on_model_select)
        ag.add_action(self._model_action)

        # Per-project harness selection (B3) — same stateful-radio pattern.
        # The value is FOLLOW_DEFAULT, or a harness id ('claude'/'opencode').
        self._harness_action = Gio.SimpleAction.new_stateful(
            'set-harness', GLib.VariantType.new('s'),
            GLib.Variant('s', FOLLOW_DEFAULT),
        )
        self._harness_action.connect('activate', self._on_harness_select)
        ag.add_action(self._harness_action)
        self._populate_harness_submenu()

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
        """Populate the Provider submenu for THIS row's effective harness.

        One native option for the harness (Anthropic / Grok / OpenCode) plus
        Claude customs when applicable. Unselectable choices are omitted —
        Gio.Menu cannot grey items. Radio state is a concrete id.
        """
        from models import (build_provider_menu_entries, provider_menu_current,
                            FOLLOW_DEFAULT as _FD)
        self._model_submenu.remove_all()
        if self._settings is not None:
            try:
                harness_id = self._settings.effective_harness(self._project.path)
                entries = build_provider_menu_entries(self._settings, harness_id)
                current = provider_menu_current(
                    self._settings, self._project.path, harness_id)
            except Exception:
                entries = [(oid, lab, True) for oid, lab in (options or [])]
                if current is None:
                    current = _FD
        else:
            entries = [(oid, lab, True) for oid, lab in (options or [])]
            if current is None:
                current = _FD
        for mid, label, _selectable in entries:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                'row.set-model', GLib.Variant('s', mid))
            self._model_submenu.append_item(item)
        if current is None:
            current = ''
        self._model_action.set_state(GLib.Variant('s', current))
        self._rebuild_popover()

    def _on_model_select(self, action, value):
        action.set_state(value)
        self.emit('project-model-change', value.get_string())

    # --- Harness submenu (B3) ----------------------------------------------

    def _populate_harness_submenu(self):
        """Build the 'Harness' submenu: one radio per registered adapter.

        The global default harness is labeled ``Display (default)``. There is no
        separate "Follow default" item — picking the default id clears any
        per-project override (window handler treats default id like FOLLOW).
        Radio state is the effective harness (override or global default).
        """
        self._harness_submenu.remove_all()
        default_id = (self._settings.harness_default
                      if self._settings is not None else harnesses.DEFAULT_HARNESS)
        for harness_id, adapter in harnesses.ADAPTERS.items():
            label = adapter.display_name
            if harness_id == default_id:
                label = f'{label} (default)'
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                'row.set-harness', GLib.Variant('s', harness_id))
            self._harness_submenu.append_item(item)
        # Effective harness is always a concrete id (never FOLLOW_DEFAULT).
        if self._settings is not None:
            current = self._settings.effective_harness(self._project.path)
        else:
            current = default_id
        self._harness_action.set_state(GLib.Variant('s', current))

    @staticmethod
    def _harness_display_name(harness_id):
        adapter = harnesses.ADAPTERS.get(harness_id)
        return adapter.display_name if adapter else harness_id

    def _on_harness_select(self, action, value):
        action.set_state(value)
        self.emit('project-harness-change', value.get_string())

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

    def set_pending_deactivate(self, pending: bool):
        """Visual grace-period state: italics + row wash + always-visible UNDO.

        While pending, strip the hover-only opacity class so UNDO stays
        visible even when the pointer leaves the row. Session state remains
        attached until the sidebar timer fires. Row class drives theme
        background wash; name label class drives italic.
        """
        self._pending_deactivate = pending
        if pending:
            self.add_css_class('project-row-pending-deactivate')
            self._name_label.add_css_class('project-row-pending-deactivate')
            # Drop hover-hide entirely (more reliable than opacity !important).
            self._actions_box.remove_css_class('project-row-actions')
            self._actions_box.add_css_class('project-row-actions-pending')
            self._deactivate_btn.set_visible(False)
            self._undo_btn.set_visible(True)
        else:
            self.remove_css_class('project-row-pending-deactivate')
            self._name_label.remove_css_class('project-row-pending-deactivate')
            self._actions_box.remove_css_class('project-row-actions-pending')
            self._actions_box.add_css_class('project-row-actions')
            self._undo_btn.set_visible(False)
            self._deactivate_btn.set_visible(True)
            self._deactivate_btn.set_sensitive(self._process_state == 'attached')

    def set_process_state(self, state: str, is_zellij: bool = None):
        """state: 'inactive' | 'attached' | 'detached'"""
        self._process_state = state
        if is_zellij is not None:
            self._is_zellij = is_zellij
        elif state == 'detached':
            self._is_zellij = True
        elif state == 'inactive':
            self._is_zellij = False
            if self._pending_deactivate:
                self.set_pending_deactivate(False)
        if not self._pending_deactivate:
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
        self._rename_active = True
        self._rename_ignore_leave = True
        self._rename_entry.set_text(self._project.name)
        # Hide the whole name stack (label + harness subtitle) so the entry
        # gets the full row width.
        self._name_box.set_visible(False)
        self._rename_entry.set_visible(True)
        # Defer grab_focus so the closing context-menu popover doesn't win the
        # focus fight; then arm leave-to-cancel only after settle.
        def _focus():
            if not self._rename_active:
                return False
            self._rename_entry.grab_focus()
            self._rename_entry.select_region(0, -1)

            def _arm_leave():
                if self._rename_active:
                    self._rename_ignore_leave = False
                return False
            # 200ms covers popover popdown + focus restore chatter on GTK4/Adw.
            GLib.timeout_add(200, _arm_leave)
            return False
        GLib.idle_add(_focus)

    def _exit_rename_mode(self):
        self._rename_active = False
        self._rename_ignore_leave = True
        self._rename_entry.set_visible(False)
        self._name_box.set_visible(True)
        self._name_label.set_visible(True)
        # Subtitle visibility is derived; refresh in case harness text applies.
        if hasattr(self, '_update_subtitle'):
            self._update_subtitle()

    def _on_rename_focus_leave(self, *_args):
        if self._rename_ignore_leave or not self._rename_active:
            return
        self._exit_rename_mode()

    def _on_rename_activate(self, entry):
        name = entry.get_text().strip()
        valid = (name and '/' not in name
                 and not name.startswith('.') and name != self._project.name)
        # Ignore leave while we tear down (activate moves focus).
        self._rename_ignore_leave = True
        self._exit_rename_mode()
        if valid:
            self.emit('project-rename', name)

    def _on_rename_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._rename_ignore_leave = True
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
    """A restorable past session, rendered from an ``harnesses.SessionRef`` (A1).

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
