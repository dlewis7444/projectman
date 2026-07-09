import os
import subprocess
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GLib

from sidebar import Sidebar
from terminal import TerminalView
from archive_window import ArchiveWindow
from shutdown_window import ShutdownWindow
from model import Project
from session import (
    save_session, load_session, load_agents, filter_active_paths,
    collect_session_state, collect_agents_map, plan_restore,
    plan_emergency_kill, should_quit_app, should_save_session, SESSION_FILE,
)


class AppWindow(Adw.ApplicationWindow):
    def __init__(self, app, store, history, watcher, settings, zellij_watcher, version='', paa_ledger=None, paa_monitor=None):
        super().__init__(application=app)
        self._store = store
        self._history = history
        self._watcher = watcher
        self._settings = settings
        self._version = version
        self._terminals = {}
        self._active_path = None
        # Per-project agent map populated from session.json during a restore
        # pass (saved-agent-wins, A2). _get_or_create_terminal consults it when
        # no explicit agent_id is passed, so the focused project — activated via
        # the normal _on_project_activated path — still recreates its saved
        # agent. Empty outside a restore; new activations fall through to
        # settings.effective_agent.
        self._restore_agents: dict = {}
        # Distinct missing-agent ids already toasted (A6 one-shot dedup).
        self._warned_agents: set = set()
        # (project_path, agent_id) pairs already toasted for a spawn failure
        # (M-UX.10a one-shot dedup — a restore storm of the same miss is one toast).
        self._warned_spawn_fail: set = set()
        # FB-2 (C7/C8): did ANY agent child actually start this run? Set in the
        # process-started handler. The close-time save consults this so a FAILED
        # restore (nothing ever ran) can't overwrite the last good session.json
        # with an empty open_paths — the session-erasure guard.
        self._any_started_this_run = False
        self._mru = []          # most-recently-used project paths, index 0 = current
        self._archive_win = None
        self._settings_win = None
        self._paa_win = None
        self._prev_status: dict = {}
        self._zellij_watcher = zellij_watcher
        self._paa_ledger = paa_ledger
        self._paa_monitor = paa_monitor
        self._paa_prev_count = paa_ledger.pending_count if paa_ledger else 0
        # reveal-3 item 2 (G2, C10): the throb is level-truthful, not edge-only.
        # _paa_unseen starts True so standing/restart-surviving pending findings
        # arm the indicator on the first emission; it flips False once the user
        # opens the PAA window (they have now SEEN the current findings).
        self._paa_unseen = True
        if paa_monitor:
            paa_monitor.connect('findings-changed', self._on_paa_findings_changed)
            paa_monitor.connect('scan-progress', self._on_paa_scan_progress)
            # M-UX.4: surface the Haiku-Check outcome — refused (guard) or done.
            paa_monitor.connect('scan-blocked', self._on_paa_scan_blocked)
            paa_monitor.connect('single-scan-complete', self._on_paa_single_scan_complete)
        zellij_watcher.connect('sessions-changed', self._on_zellij_sessions_changed)

        self.set_default_size(1200, 750)
        self.set_title('ProjectMan')
        self.set_icon_name('io.github.projectman')

        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title='ProjectMan', subtitle='')
        self._header.set_title_widget(self._title)

        sidebar_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        projects_lbl = Gtk.Label(label='PROJECTS')
        projects_lbl.add_css_class('pm-sidebar-title')
        sidebar_head.append(projects_lbl)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text('Filter…')
        # FB-7 tooltip audit: the only header widget that lacked one.
        self._search_entry.set_tooltip_text('Filter projects by name')
        self._search_entry.set_max_width_chars(14)
        self._search_entry.connect('search-changed', self._on_search_changed)
        self._search_entry.connect('stop-search', self._on_search_stop)
        sidebar_head.append(self._search_entry)

        self._pin_btn = Gtk.ToggleButton()
        self._pin_btn.set_active(True)
        self._pin_btn.set_icon_name('sidebar-show-symbolic')
        self._pin_btn.add_css_class('flat')
        self._pin_btn.set_tooltip_text('Pin sidebar')
        self._pin_btn.connect('toggled', self._on_sidebar_pin_toggled)
        sidebar_head.append(self._pin_btn)
        self._header.pack_start(sidebar_head)

        toolbar_view.add_top_bar(self._header)

        self._sidebar_pos = settings.sidebar_width
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(settings.sidebar_width)
        self._paned.set_resize_start_child(False)
        self._paned.set_shrink_start_child(False)
        self._paned.connect('notify::position', self._on_paned_position_notify)

        self._sidebar = Sidebar(store, history, watcher, version=self._version,
                                settings=settings)
        self._sidebar.set_ntfy_enabled(settings.ntfy_enabled)
        self._sidebar.connect('project-activated',   self._on_project_activated)
        self._sidebar.connect('session-activated',   self._on_session_activated)
        self._sidebar.connect('project-archive',     self._on_project_archive)
        self._sidebar.connect('project-deactivate',  self._on_project_deactivate)
        self._sidebar.connect('project-new-session', self._on_project_new_session)
        self._sidebar.connect('project-zellij',      self._on_project_open_zellij)
        self._sidebar.connect('project-ntfy-toggle', self._on_ntfy_toggle)
        self._sidebar.connect('project-haiku-check', self._on_project_haiku_check)
        self._sidebar.connect('project-model-change', self._on_project_model_change)
        self._sidebar.connect('show-archive-window', self._on_show_archive_window)
        self._sidebar.connect('show-settings',       self._on_open_settings)
        self._sidebar.connect('project-create', self._on_project_create)
        self._sidebar.connect('project-rename', self._on_project_rename)
        self._sidebar.connect('show-paa-window', self._on_show_paa_window)
        self._paned.set_start_child(self._sidebar)

        self._stack = Gtk.Stack()
        placeholder = Adw.StatusPage()
        placeholder.set_title('Select a Project')
        placeholder.set_description(
            'Select a project in the sidebar to start a session'
        )
        placeholder.set_icon_name('folder-symbolic')
        self._stack.add_named(placeholder, '__placeholder__')
        self._paned.set_end_child(self._stack)

        # ToastOverlay wraps the whole content area so fallback notices for the
        # provider-unavailable guard float above the terminal without blocking it.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._paned)
        toolbar_view.set_content(self._toast_overlay)
        self.set_content(toolbar_view)

        # Provider-fallback toast aggregation state: pending (project_name,
        # reason) events batched for ~2s before display; at most ONE provider
        # toast shown at a time.
        self._provider_pending: list = []    # buffered (name, reason) pairs
        self._provider_toast_timer = None    # GLib.timeout_add source id | None
        self._provider_toast: Adw.Toast | None = None  # currently displayed toast

        watcher.connect('status-changed', self._on_status_changed)
        self.connect('close-request', self._on_close_request)
        self._sidebar.start_polling()
        # Defensive fallback. TerminalView now runs its own pidfd watch at
        # G_PRIORITY_DEFAULT alongside vte's G_PRIORITY_LOW reaper
        # (terminal.py:_add_pidfd_watch); whichever wins handles the exit.
        # This poll exists as belt-and-suspenders if both ever miss, and
        # should rarely fire.
        GLib.timeout_add_seconds(5, self._sweep_dead_terminals)
        self._setup_shortcuts()
        self._refresh_sidebar_models()

    def _sweep_dead_terminals(self):
        for path, tv in list(self._terminals.items()):
            before = tv._child_pid
            tv.check_child_alive()
            # If check_child_alive cleared _child_pid, both vte's reaper and
            # our pidfd watch missed this exit. Flash the row so we know
            # it's happening (and how often).
            if before is not None and tv._child_pid is None:
                self._sidebar.flash_sweeper_caught(path)
        return GLib.SOURCE_CONTINUE

    def _on_zellij_sessions_changed(self, watcher):
        """A session appeared or disappeared — reconcile sidebar state."""
        if self._settings.multiplexer != 'zellij':
            return
        import zellij as z
        for project in self._store.load_projects():
            path = project.path
            sname = z.session_name(project.name)
            tv = self._terminals.get(path)
            currently_attached = tv is not None and tv._child_pid is not None
            if currently_attached:
                continue  # process-exited will handle this case
            if z.session_alive(sname):
                self._sidebar.set_project_state(path, 'detached')
            else:
                self._sidebar.set_project_state(path, 'inactive')

    def _on_close_request(self, window):
        self._settings.sidebar_width = self._sidebar_pos
        self._settings.save()
        if self._paa_win is not None:
            self._paa_win.destroy()
            self._paa_win = None
        running = {path: tv for path, tv in self._terminals.items()
                   if tv._child_pid is not None}
        if not running:
            self._save_session()      # write empty session; restore is a no-op
            return False

        # If any session is actively working (orange dot), confirm first
        working_names = [
            self._find_project(p).name
            for p in running
            if (proj := self._find_project(p)) and
               self._watcher.get_project_status(proj) == 'working'
        ]
        if working_names:
            self._show_working_confirm(running, working_names)
        else:
            self._open_shutdown_window(running)
        return True  # prevent immediate close — shutdown window drives the close

    def _show_working_confirm(self, running, working_names):
        names_str = '\n'.join(f'\u2022 {n}' for n in working_names)
        dialog = Adw.AlertDialog.new(
            'Interrupt Active Work?',
            f'Work is currently in progress on:\n{names_str}\n\n'
            f'Closing ProjectMan may interrupt incomplete operations.',
        )
        dialog.add_response('cancel', 'Keep Running')
        dialog.add_response('close', 'Close Anyway')
        dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def on_response(d, response_id):
            if response_id == 'close':
                self._open_shutdown_window(running)

        dialog.connect('response', on_response)
        dialog.present(self)

    def _open_shutdown_window(self, running):
        self._save_session()      # snapshot before SIGTERM
        ShutdownWindow(parent=self, running=running, on_complete=self._quit)

    def _quit(self):
        """Destroy this window; quit the app only if it is the primary one.

        Closing a stray/duplicate window must not take the whole app (and its
        unrelated sessions) down — gated by ``should_quit_app``. When this IS
        the primary window, clear ``app._window`` BEFORE quitting so a racing
        re-activation cannot present a destroyed window.
        """
        app = self.get_application()
        quit_app = should_quit_app(getattr(app, '_window', None), self)
        if quit_app and app is not None:
            app._window = None
        self.destroy()
        if quit_app and app is not None:
            app.quit()

    def emergency_shutdown(self):
        """Non-interactive fast-path teardown for SIGTERM/SIGHUP.

        NO dialogs, NO ShutdownWindow. Ordering contract: save → kill.
        1. Save the session FIRST so the snapshot lands even if a later kill
           hangs.
        2. Kill the live DIRECT-spawn children (process groups, via the
           terminal's own ``_kill_child``). Zellij terminals are NEVER killed —
           their sessions persist by design.
        Returns the list of killed paths (for the signal handler / logging).
        Never prompts, never waits on children.
        """
        self._save_session()
        killed = plan_emergency_kill(self._terminals)
        for path in killed:
            self._terminals[path]._kill_child()
        return killed

    def _save_session(self):
        """Snapshot running terminals to SESSION_FILE (atomic write).

        Persists the per-project agent id alongside each path (session.json v2)
        so a future restore re-spawns the right agent. The persisted agent is
        the one the terminal is RUNNING (spawn-time truth via
        spawned_agent_signature(), the same source the restart prompt reads),
        not the one settings would pick today — a saved-agent-wins restore (A2)
        legitimately diverges from settings, and re-saving the settings agent
        would silently drop that session on the next restore. For an all-claude
        fleet the dict form is written regardless; the v2 loader reads both
        forms.
        """
        if not self._settings.resume_projects:
            return
        open_paths, focused = collect_session_state(self._terminals, self._active_path)
        # FB-2 (C7/C8): the session-erasure guard. If NOTHING started this run and
        # the existing session.json still holds open paths, a failed restore is
        # about to overwrite the last good session with an empty list — SKIP the
        # write and say so (one stderr line). Anything that started this run, or
        # an already-empty existing session, saves normally.
        if not self._any_started_this_run:
            existing_open, _ = load_session(SESSION_FILE)
            if not should_save_session(self._any_started_this_run, existing_open):
                print(
                    'ProjectMan: nothing started this run; preserving the '
                    f'existing session ({len(existing_open)} project(s)) '
                    'instead of overwriting it.',
                    file=sys.stderr,
                )
                return
        agents_map = collect_agents_map(
            self._terminals, open_paths, self._settings.effective_agent)
        save_session(SESSION_FILE, open_paths, focused, agents=agents_map)

    def _restore_session(self):
        """Restore projects that were running at the last committed close."""
        if self._settings.multiplexer == 'zellij':
            self._restore_zellij_session()
            return
        # --- direct-agent mode (original behaviour) ---
        if not self._settings.resume_projects:
            return
        open_paths, focused_path = load_session(SESSION_FILE)
        # saved-agent-wins on restore (A2): recreate the agent each project was
        # actually running, overriding settings.effective_agent. v1 (str) and
        # agent-less entries default to claude inside load_agents.
        self._restore_agents = load_agents(SESSION_FILE)
        active = filter_active_paths(open_paths, self._store.load_projects())
        focused, background = plan_restore(open_paths, focused_path, active)
        self._sidebar.set_active_only(bool(active))
        try:
            if focused:
                self._on_project_activated(self._sidebar, focused)
            for path in background:
                project = active[path]
                tv = self._get_or_create_terminal(project)
                if tv._child_pid is None:
                    tv.spawn_continue(project_name=project.name)
        finally:
            self._restore_agents = {}

    def _restore_zellij_session(self):
        """In zellij mode: find live pm-* sessions, mark detached, re-open last-focused.

        Falls back to session.json when no live sessions exist (e.g. first run after
        switching from direct-claude mode, or after a system reboot that cleared sessions).
        _on_project_activated decides per-project whether to attach zellij or spawn claude.
        """
        import zellij as z
        alive_names = z.alive_session_names()
        live = []
        for project in self._store.load_projects():
            sname = z.session_name(project.name)
            if sname in alive_names:
                self._sidebar.set_project_state(project.path, 'detached')
                live.append(project)

        if not self._settings.resume_projects:
            self._sidebar.set_active_only(bool(live))
            return

        open_paths, focused_path = load_session(SESSION_FILE)
        # saved-agent-wins on restore (A2): the same map the direct path uses.
        self._restore_agents = load_agents(SESSION_FILE)
        all_paths = {p.path for p in self._store.load_projects()}

        restore_path = focused_path if focused_path and focused_path in all_paths else None
        if restore_path is None:
            for path in open_paths:
                if path in all_paths:
                    restore_path = path
                    break

        background = [p for p in open_paths if p != restore_path and p in all_paths]
        self._sidebar.set_active_only(bool(live) or bool(restore_path))

        try:
            if restore_path:
                self._on_project_activated(self._sidebar, restore_path)

            for path in background:
                project = self._find_project(path)
                if not project:
                    continue
                tv = self._get_or_create_terminal(project)
                if tv._child_pid is None:
                    sname = z.session_name(project.name)
                    if sname in alive_names:
                        tv.spawn_zellij(sname)
                    else:
                        tv.spawn_continue(project_name=project.name)
        finally:
            self._restore_agents = {}

    def _push_mru(self, path):
        self._mru = [path] + [p for p in self._mru if p != path]

    def _setup_shortcuts(self):
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_F5:
            return self._on_f5()
        if ctrl and keyval == Gdk.KEY_Tab:
            return self._on_ctrl_tab()
        return False

    def _on_ctrl_tab(self):
        self._debug(f'ctrl+tab mru={[os.path.basename(p) for p in self._mru]}')
        if len(self._mru) >= 2:
            self._switch_to_project(self._mru[1])
        return True

    def _debug(self, msg):
        if self._settings.debug_logging:
            print(f'[DBG] {msg}', flush=True)

    def _set_active_project(self, name):
        self._title.set_subtitle(name or '')

    def _on_search_changed(self, entry):
        self._sidebar.set_filter_text(entry.get_text())

    def _on_search_stop(self, entry):
        entry.set_text('')
        if self._active_path and self._active_path in self._terminals:
            self._terminals[self._active_path].get_terminal().grab_focus()

    def _on_sidebar_pin_toggled(self, btn):
        pinned = btn.get_active()
        self._search_entry.set_visible(pinned)
        if pinned:
            self._paned.set_shrink_start_child(False)
            self._paned.set_position(self._sidebar_pos)
        else:
            self._search_entry.set_text('')
            self._sidebar_pos = self._paned.get_position()
            self._paned.set_shrink_start_child(True)
            self._paned.set_position(0)

    def _on_paned_position_notify(self, paned, _param):
        if self._pin_btn.get_active():
            self._sidebar_pos = paned.get_position()

    def _on_f5(self):
        if self._active_path and self._active_path in self._terminals:
            project = self._find_project(self._active_path)
            pname = project.name if project else None
            self._terminals[self._active_path].spawn_continue(project_name=pname)
        return True

    def _show_toast(self, text, timeout=5):
        """Fire a one-shot toast (M-UX.3/10/11 share this).

        Plain wrapper over the overlay so failure-surface handlers don't each
        reimplement Toast plumbing; the provider fallback path keeps its own
        aggregating helper (it batches + dedups, this does not)."""
        toast = Adw.Toast.new(text)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    def _spawn_failure_toast_text(self, agent_id, raw_binary):
        """The M-UX.10a toast string: '<binary> not found — <display> isn't
        installed. <install hint>'.

        Resolves the agent's REAL binary + display name + install hint from the
        adapter (the raw argv[0] can be 'bash' under the continue wrapper, so we
        prefer the adapter's binary). Pure string assembly; no GTK."""
        import agents
        adapter = agents.ADAPTERS.get(agent_id) or agents.get_adapter(agent_id, self._settings)
        display = getattr(adapter, 'display_name', agent_id)
        hint = getattr(adapter, 'install_hint', '')
        binary = agent_id
        try:
            if agent_id == 'claude':
                binary = self._settings.resolved_claude_binary
            elif hasattr(adapter, '_binary'):
                binary = adapter._binary(self._settings)
        except Exception:
            pass
        # If the raw argv[0] was a concrete path (not the bash wrapper), prefer it.
        if raw_binary and os.path.basename(raw_binary) not in ('bash', 'sh', ''):
            binary = raw_binary
        msg = f"{binary} not found — {display} isn't installed."
        if hint:
            msg = f"{msg} {hint}"
        return msg

    def _on_spawn_failed(self, project_path, agent_id, raw_binary):
        """M-UX.10a (C7): a missing-binary spawn → one-shot install-hint toast.

        The row is NOT removed here — process-exited already set it 'inactive'
        (kept visible because set_active_only no longer fires on the activation
        attempt, M-UX.10b). This adds the in-app explanation + recovery path the
        triple-whammy lacked. Warned once per (project, agent) so a restore storm
        doesn't stack toasts.

        A failure ALWAYS reveals the board (C7): restore arms "Active Only"
        eagerly for the successful case, but a spawn that FAILS would then leave
        the user with LESS UI — the just-failed 'inactive' row hidden behind a
        filter they didn't set. So a failed spawn drops the filter, every time
        (idempotent — the toast is one-shot, this is not, and dropping an
        already-off filter is a no-op). Restore's eager filter for the
        successful path is untouched."""
        self._sidebar.set_active_only(False)
        key = (project_path, agent_id)
        if key in self._warned_spawn_fail:
            return
        self._warned_spawn_fail.add(key)
        # FB-10 (RB-1, H2): the spawn-failure toast is PERSISTENT (timeout=0, like
        # the provider fallback toast). RB-1's repro proved the mechanism fired — but
        # at timeout=5 it auto-dismissed before an unfocused user looked, reading
        # as "no toast". A missing-binary failure (C7) needs a recovery hint that
        # waits to be read. Dedup (_warned_spawn_fail) is unchanged.
        self._show_toast(self._spawn_failure_toast_text(agent_id, raw_binary),
                         timeout=0)

    def _handle_late_death(self, tv, status, path):
        """FB-3 (C7): a child exited — surface it in the pane.

        Feeds a one-line 'session ended — exit <code>' banner into the pane
        (display-only) so a late/external death doesn't leave a frozen,
        unexplained terminal. The exit code is derived from the raw wait
        status; an undecodable status falls back to the raw number.

        The Active Only filter is NOT touched here. An earlier version dropped
        the filter when the active project died so its row wouldn't vanish
        behind it — but that auto-toggled a filter the user had set, which was
        worse (the maintainer 2026-06-18: closing a session should leave the filter
        alone). The row going inactive hides as the filter intends; the user
        toggles it off themselves.
        """
        try:
            code = os.waitstatus_to_exitcode(status)
        except (ValueError, ChildProcessError):
            code = status
        tv.feed_session_ended(code)

    def _show_provider_fallback_toast(self, project_name, reason):
        """Enqueue a fallback notice for aggregation; flush after a ~2s window.

        Multiple projects failing within the same restore batch fire within
        milliseconds of each other. Batching them for 2s lets the aggregator
        collapse N identical-reason events into one toast instead of N toasts
        dismissed one-by-one (UX). There is at most ONE provider toast in the
        overlay at any time; a new aggregate dismisses any still-displayed one
        and re-adds rather than queueing (persistent timeout(0), spec §6).
        """
        self._provider_pending.append((project_name, reason))
        # Arm (or re-arm) the 2s flush timer; each new event resets the window
        # so closely spaced starts all land in the same batch.
        if self._provider_toast_timer is not None:
            GLib.source_remove(self._provider_toast_timer)
        self._provider_toast_timer = GLib.timeout_add(2000, self._flush_provider_toast)

    def _flush_provider_toast(self):
        """Collapse pending provider fallback events and show one toast."""
        from models import aggregate_fallback_notices
        self._provider_toast_timer = None
        events = list(self._provider_pending)
        self._provider_pending.clear()
        if not events:
            return GLib.SOURCE_REMOVE
        text = aggregate_fallback_notices(events)
        # aggregate_fallback_notices returns str or list[str]; for the window
        # we always show a single toast — join multiple reasons with a separator.
        if isinstance(text, list):
            text = ' | '.join(text)
        if not text:
            return GLib.SOURCE_REMOVE
        # Dismiss any still-displayed provider toast before adding the new one so
        # it replaces rather than queues.
        if self._provider_toast is not None:
            self._provider_toast.dismiss()
            self._provider_toast = None
        toast = Adw.Toast.new(text)
        toast.set_timeout(0)   # persistent until the user dismisses (spec §6)
        toast.connect('dismissed', self._on_provider_toast_dismissed)
        self._provider_toast = toast
        self._toast_overlay.add_toast(toast)
        return GLib.SOURCE_REMOVE

    def _on_provider_toast_dismissed(self, _toast):
        """Clear the live-toast reference when the user dismisses it."""
        self._provider_toast = None

    def _maybe_warn_unknown_agent(self, agent_id):
        """One-shot toast when a NAMED harness isn't available.

        ``resolve_adapter`` returns a non-None ``missing`` only when a non-empty
        id isn't the registered harness ('claude'); the spawn still proceeds on
        the fallback (always claude now). The toast names the harness that will
        ACTUALLY run rather than saying nothing.
        The warning is shown once per distinct missing id (``_warned_agents``)
        so a multi-project restore naming the same dead agent doesn't toast N
        times.
        """
        import agents
        adapter, missing = agents.resolve_adapter(agent_id, self._settings)
        if not missing or missing in self._warned_agents:
            return
        self._warned_agents.add(missing)
        toast = Adw.Toast.new(
            f"harness '{missing}' not available — using {adapter.display_name}"
        )
        toast.set_timeout(5)
        self._toast_overlay.add_toast(toast)

    def _get_or_create_terminal(self, project, agent_id=None):
        """Return the project's TerminalView, creating it if absent.

        ``agent_id`` is the explicit agent for a freshly created terminal —
        used by restore to recreate the agent that was actually running
        (saved-agent-wins, A2). It is ignored once a terminal exists (a live
        terminal keeps its agent). New non-restore activations pass None and
        the terminal follows ``settings.effective_agent``.

        When ``agent_id`` is None, the in-progress restore map
        (``_restore_agents``) is consulted so the focused project — which is
        activated through the ordinary _on_project_activated path rather than
        with an explicit id — still recreates its saved agent.
        """
        if agent_id is None:
            agent_id = self._restore_agents.get(project.path)
        if project.path not in self._terminals:
            # A6/m3: if the project's agent is NAMED but not registered (a stale
            # session/settings id, or a typo), warn once. The terminal still
            # falls back to claude — resolve_adapter only changes the diagnostic.
            effective = (agent_id if agent_id is not None
                         else self._settings.effective_agent(project.path))
            self._maybe_warn_unknown_agent(effective)
            tv = TerminalView(project, self._settings, agent_id=agent_id)

            def _on_started(t, p=project.path, n=project.name):
                # FB-2: a child actually ran this run → the close-time save is now
                # authorized to overwrite session.json (a failed restore never
                # reaches here, so it can't erase the last good session).
                self._any_started_this_run = True
                self._sidebar.set_project_state(p, 'attached', is_zellij=t._is_zellij)
                # C5: tell the row which agent the child is ACTUALLY running, so a
                # restored saved-agent-wins session (A2) whose live agent differs
                # from the configured one shows the truth, not the next-session
                # agent. Spawn-time truth via spawned_agent_signature() — the same
                # source the restart prompt reads.
                self._sidebar.set_running_agent(p, t.spawned_agent_signature())
                # M-UX.10b (C7): the auto "Active Only" filter engages only once
                # something actually RUNS — NOT on the activation attempt. This is
                # what keeps a failed-spawn row visible (a spawn that never starts
                # never flips the filter, so the 'inactive' row isn't hidden).
                self._sidebar.set_active_only(True)
                # Surface provider fallback notice if this spawn fell back to native.
                if t._fallback_reason:
                    self._show_provider_fallback_toast(n, t._fallback_reason)

            def _on_exited(t, s, p=project.path):
                self._sidebar.set_project_state(p, 'inactive', is_zellij=False)
                # No live child → no running agent; clear the C5 mismatch subtitle.
                self._sidebar.set_running_agent(p, None)
                # FB-3 (C7): a dying child must not leave a frozen, unexplained
                # pane — feed a "session ended" banner. The Active Only filter
                # is left untouched (see _handle_late_death).
                self._handle_late_death(t, s, p)

            def _on_detached(t, p=project.path):
                self._sidebar.set_project_state(p, 'detached', is_zellij=True)
                # Detached to a multiplexer — no live child here whose agent we
                # can attest; drop the C5 mismatch subtitle (detach-to-none).
                self._sidebar.set_running_agent(p, None)

            tv.connect('process-started', _on_started)
            tv.connect('process-exited', _on_exited)
            tv.connect('process-detached', _on_detached)
            tv.connect('process-spawn-failed',
                       lambda t, b, p=project.path: self._on_spawn_failed(p, t._adapter.id, b))
            self._terminals[project.path] = tv
            self._stack.add_named(tv, project.path)
        return self._terminals[project.path]

    def _sync_running_state(self):
        """Re-apply process running flags after a sidebar refresh."""
        for path, tv in self._terminals.items():
            if tv._child_pid is not None:
                self._sidebar.set_project_state(path, 'attached', is_zellij=tv._is_zellij)

    def _find_project(self, path):
        for p in self._store.load_projects() + self._store.load_archived():
            if p.path == path:
                return p
        return None

    # --- project activation ---

    def _switch_to_project(self, path):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._set_active_project(project.name)
        self._active_path = path
        self._push_mru(path)
        self._sidebar.select_project(path)
        if tv._child_pid is None:
            import zellij as z
            sname = z.session_name(project.name)
            if z.session_alive(sname):
                tv.spawn_zellij(sname)
            else:
                tv.spawn_continue(project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_project_activated(self, sidebar, path):
        if self._search_entry.get_text():
            self._search_entry.set_text('')
        # M-UX.10b (C7): do NOT enable "Active Only" here. The auto-filter now
        # fires from the process-started handler (_on_started) — i.e. only once a
        # child actually runs — so a spawn that FAILS (missing binary) leaves the
        # row visible in 'inactive' instead of being hidden by an eagerly-set
        # filter (the triple-whammy's third blow). The manual toggle is untouched.
        self._switch_to_project(path)

    def _on_session_activated(self, sidebar, path, session_id):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._set_active_project(project.name)
        self._active_path = path
        self._push_mru(path)
        tv.spawn_resume(session_id, project_name=project.name)
        tv.get_terminal().grab_focus()

    # --- deactivate (kill process, keep in sidebar as inactive) ---

    def _on_project_deactivate(self, sidebar, path):
        tv = self._terminals.get(path)
        if tv is None:
            return
        if tv._is_zellij:
            import zellij as z
            project = self._find_project(path)
            if project:
                sname = z.session_name(project.name)
                # Clear zellij flags BEFORE killing the session so that
                # _on_child_exited emits process-exited (not process-detached).
                # Without this, a race exists: the VTE child may exit before
                # zellij finishes cleaning up the session socket, causing
                # session_alive() to return True and the project to stay
                # visible as "detached" in the Active list.
                existed = z.session_exists(sname)
                tv._is_zellij = False
                tv._zellij_session = None
                if existed:
                    z.kill_session(sname)   # FB-4: the shared kill helper
                    if tv._child_pid is None:
                        self._sidebar.set_project_state(path, 'inactive')
        else:
            tv.deactivate()
            # process-exited signal fires → set_project_state(path, 'inactive')

    # --- archive (move to .archive, remove terminal) ---

    def _on_project_archive(self, sidebar, path):
        if path in self._terminals:
            tv = self._terminals.pop(path)
            tv._kill_child()
            self._stack.remove(tv)
        project = self._find_project(path)
        if project:
            if self._settings.multiplexer == 'zellij':
                import zellij as z
                z.kill_session(z.session_name(project.name))  # FB-4 shared helper
            self._store.archive(project)
        self._sidebar.refresh()
        self._sync_running_state()
        if self._active_path == path:
            self._stack.set_visible_child_name('__placeholder__')
            self._active_path = None
            self._set_active_project(None)

    # --- archive popup ---

    def _on_show_archive_window(self, sidebar):
        if self._archive_win is not None:
            self._archive_win.present()
            return
        self._archive_win = ArchiveWindow(
            parent=self,
            store=self._store,
            on_restore=self._on_archived_project_restored,
        )
        self._archive_win.connect('destroy', lambda w: setattr(self, '_archive_win', None))
        self._archive_win.present()

    def _on_archived_project_restored(self, project):
        self._store.restore(project)
        self._sidebar.refresh()
        self._sync_running_state()

    def _on_show_paa_window(self, sidebar):
        self._sidebar.stop_paa_throb()
        # G2 (C10): opening the window means the user has SEEN the current
        # findings — they no longer count as unseen, so a later same-count
        # emission must not re-throb (paa_throb_decision's unseen gate).
        self._paa_unseen = False
        if self._paa_win is not None:
            self._paa_win.present()
            return
        if self._paa_ledger is None:
            return
        from paa_card_window import PAACardWindow
        self._paa_win = PAACardWindow(
            parent=self,
            ledger=self._paa_ledger,
            settings=self._settings,
            store=self._store,
            on_close=lambda: setattr(self, '_paa_win', None),
            on_action=self._on_paa_card_action,
        )
        self._paa_win.present()

    def _on_paa_card_action(self, pending_count):
        """Card dismissed/acknowledged — update count but don't throb."""
        self._sidebar.set_paa_pending_count(pending_count)

    def _on_paa_findings_changed(self, monitor, pending_count):
        # set_paa_pending_count handles the stop-on-zero path (sidebar.py).
        self._sidebar.set_paa_pending_count(pending_count)
        # G2 (C10): the throb is decided by the level-truthful unseen-pending
        # rule, not the old edge-only count>prev gate that standing/restart
        # findings could never trip. Pure decision in session.py.
        from session import paa_throb_decision
        throb, self._paa_unseen = paa_throb_decision(
            pending_count, self._paa_prev_count,
            self._paa_unseen, self._paa_win is not None)
        if throb:
            self._sidebar.start_paa_throb()
        self._paa_prev_count = pending_count
        if self._paa_win is not None:
            self._paa_win.refresh_from_scan()

    def _on_project_haiku_check(self, sidebar, path):
        if self._paa_monitor is None:
            return
        project = self._find_project(path)
        if project:
            # The paa_enabled AND paa_allow_haiku guard now lives INSIDE
            # scan_single_project (M-UX.4) — a disabled scan makes zero model
            # calls and emits 'scan-blocked' (→ toast) instead of silently
            # billing Anthropic.
            self._paa_monitor.scan_single_project(project.name, project.path)

    def _on_paa_scan_blocked(self, monitor, reason):
        """M-UX.4 (C6): the Haiku Check was refused — say why, loudly."""
        self._show_toast(reason)

    def _on_paa_single_scan_complete(self, monitor, project_name, new_findings):
        """M-UX.4 (C6): show the Haiku Check RESULT (the sweep saw none)."""
        if new_findings > 0:
            msg = (f'Scan of {project_name}: {new_findings} new '
                   f'{"finding" if new_findings == 1 else "findings"}')
        else:
            msg = f'Scan of {project_name}: no new findings'
        self._show_toast(msg)

    def _on_paa_scan_progress(self, monitor, names):
        self._sidebar.set_paa_scanning(names)
        if self._paa_win is not None:
            self._paa_win.set_scanning(names)

    # --- other terminal actions ---

    def _on_project_new_session(self, sidebar, path):
        project = self._find_project(path)
        if not project:
            return
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._set_active_project(project.name)
        self._active_path = path
        self._push_mru(path)
        tv.spawn_fresh(project_name=project.name)
        tv.get_terminal().grab_focus()

    def _on_project_open_zellij(self, sidebar, path):
        """Explicit 'Open in Zellij' — always create/attach zellij session."""
        if self._settings.multiplexer != 'zellij':
            # M-UX.3 (C6): "New Zellij Session" used to silently no-op (a brief
            # spinner, then nothing) when the multiplexer wasn't zellij. Say why,
            # and do NOT spawn (no spinner): the action that does nothing explains
            # itself. This is the constitution's own origin case for C6.
            self._show_toast(
                'Zellij is disabled (Settings → Terminal → Multiplexer)')
            return
        project = self._find_project(path)
        if not project:
            return
        import zellij as z
        tv = self._get_or_create_terminal(project)
        self._stack.set_visible_child_name(path)
        self._set_active_project(project.name)
        self._active_path = path
        self._push_mru(path)
        sname = z.session_name(project.name)
        if not (tv._child_pid is not None and tv._is_zellij):
            tv.spawn_zellij(sname)
        tv.get_terminal().grab_focus()

    def apply_settings(self, settings):
        """Apply updated settings to all running terminals."""
        self._settings = settings
        for tv in self._terminals.values():
            tv.apply_settings(settings)
        # Push settings into the sidebar so per-row caps gating + the session
        # source follow the (possibly changed) effective agent (A1/A5).
        self._sidebar.set_settings(settings)
        self._sidebar.set_ntfy_enabled(settings.ntfy_enabled)
        self._refresh_sidebar_models()

    def _refresh_sidebar_models(self):
        """Push the current provider options into the sidebar menus."""
        from models import build_provider_options, provider_label
        ids, labels = build_provider_options(self._settings.providers)
        options = list(zip(ids, labels))
        # The "Default (…)" item names the active provider's label. Each row
        # derives its own story from its effective provider (a per-project
        # override to a different provider must not wear the global default's
        # story); this global label is the fallback for settings-less/legacy
        # rows.
        global_label = provider_label(
            self._settings.providers, self._settings.model_default)
        self._sidebar.set_model_options(
            options, self._settings.model_overrides, global_label)

    def _on_project_model_change(self, sidebar, path, value):
        """A per-project provider was picked from the sidebar context menu."""
        from models import FOLLOW_DEFAULT
        overrides = dict(self._settings.model_overrides)
        if value == FOLLOW_DEFAULT:
            overrides.pop(path, None)
        else:
            overrides[path] = value
        self._settings.model_overrides = overrides
        self._settings.save()
        self._refresh_sidebar_models()
        self._maybe_prompt_restart(path)

    def _maybe_prompt_restart(self, path):
        """If a live session's model (provider) just changed, offer to restart it.

        The provider/tier assignment is fixed at spawn time, so a running
        session keeps its old provider until re-spawned. Never auto-kill —
        that would lose context.

        The ``agent_stale`` branch is kept defensively but is now unreachable:
        Claude Code is the sole harness, so ``spawned_agent_signature`` always
        equals ``effective_agent`` ('claude'). It stays as a guard in case a
        future second harness is reintroduced.
        """
        tv = self._terminals.get(path)
        if tv is None or tv._child_pid is None:
            return
        model_stale = tv.spawned_model_signature() != self._settings.effective_provider(path)
        agent_stale = tv.spawned_agent_signature() != self._settings.effective_agent(path)
        if not model_stale and not agent_stale:
            return
        project = self._find_project(path)
        name = project.name if project else os.path.basename(path)
        if agent_stale and model_stale:
            title, what = 'Harness Changed', 'harness and model'
        elif agent_stale:
            title, what = 'Harness Changed', 'harness'
        else:
            title, what = 'Model Changed', 'model'
        dialog = Adw.AlertDialog.new(
            title,
            f'The new {what} for "{name}" applies to the next session. '
            f'Restart this session now?',
        )
        dialog.add_response('later', 'Apply Later')
        dialog.add_response('restart', 'Restart Now')
        dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('later')
        dialog.set_close_response('later')

        def on_response(d, response_id):
            if response_id == 'restart':
                self._on_project_new_session(self._sidebar, path)

        dialog.connect('response', on_response)
        dialog.present(self)

    def _on_open_settings(self, *args):
        if self._settings_win is not None:
            self._settings_win.present(self)
            return
        from settings_window import SettingsWindow
        self._settings_win = SettingsWindow(
            self._settings, self.get_application(), self
        )
        self._settings_win.connect(
            'closed', lambda w: setattr(self, '_settings_win', None)
        )

    def _on_ntfy_toggle(self, sidebar, path):
        pass  # state lives on ProjectRow._ntfy_action; re-checked on status change

    def _on_status_changed(self, watcher):
        self._sidebar.refresh_status()
        self._check_ntfy()

    def _check_ntfy(self):
        if not self._settings.ntfy_enabled or not self._settings.ntfy_topic:
            return
        ntfy_paths = self._sidebar.get_ntfy_active_paths()
        for path in ntfy_paths:
            project = self._find_project(path)
            if not project:
                continue
            new_state = self._watcher.get_project_status(project)
            old_state = self._prev_status.get(path, '')
            if old_state != 'done' and new_state == 'done':
                self._send_ntfy(project.name)
            self._prev_status[path] = new_state

    def _send_ntfy(self, project_name):
        topic = self._settings.ntfy_topic
        # De-Clauded payload (agent-neutral): "<project> finished".
        subprocess.Popen([
            'curl', '-s',
            '-H', f'Title: {project_name}',
            '-d', f'{project_name} finished',
            f'https://ntfy.sh/{topic}'
        ])

    def _on_project_create(self, sidebar, name):
        try:
            self._store.create_project(name)
        except OSError:
            return
        self._sidebar.refresh()
        # reveal-3 item 3 (G3, C3): drop the user straight into the new project
        # instead of leaving them on a dead pane. create_project returns None, so
        # derive the path the store's way (same derivation the B4 toast uses
        # below) and activate through the CANONICAL path — the restore precedent
        # (_on_project_activated, e.g. window.py restore at ~344/390). The B4
        # creation toast STAYS: it names the resolved agent, the spawn makes it
        # concrete.
        path = os.path.join(self._store._projects_dir(), name)
        self._on_project_activated(self._sidebar, path)
        # B4 (M-UX.15, C7-adjacent / S7): a fresh project silently inherited the
        # default agent — a "noob" who named it "claude-thing" got grok with no
        # word. Fire the M-UX.11 toast pattern naming the resolved agent (incl.
        # the missing-binary fallback) so the agent the project got is never a
        # surprise.
        self._show_toast(self._project_created_toast_text(name))

    def _project_created_toast_text(self, name):
        """The B4 creation toast: "New project '<name>'".

        Post-pivot Claude Code is the sole harness, so the old
        "— harness: <Display>" suffix was always the same value and read as
        jargon — dropped it. (The effective-harness resolution that fed the
        suffix is gone with it; reintroduce both if a second harness returns.)
        """
        return f"New project '{name}'"

    def _on_project_rename(self, sidebar, old_path, new_name):
        project = self._find_project(old_path)
        if not project:
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        try:
            self._store.rename_project(project, new_name)
        except OSError:
            return

        # Migrate terminal stack entry so the running session survives the rename
        if old_path in self._terminals:
            tv = self._terminals.pop(old_path)
            self._stack.remove(tv)
            self._stack.add_named(tv, new_path)
            self._terminals[new_path] = tv
            tv._project = Project(name=new_name, path=new_path)

        if self._active_path == old_path:
            self._active_path = new_path
            self._mru = [new_path if p == old_path else p for p in self._mru]
            self._set_active_project(new_name)

        self._sidebar.refresh()
        self._sync_running_state()
