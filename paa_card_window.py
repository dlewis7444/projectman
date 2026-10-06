import os
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Adw, Gdk, GLib, Vte, Pango

from settings import match_vte_key_capture
from terminal import _TERMINAL_PALETTES
from paa_findings import TYPE_LABELS as _TYPE_LABELS, discuss_finding_prompt

_SEVERITY_CSS = {
    'info': 'paa-card-type-info',
    'warning': 'paa-card-type-warning',
    'action-needed': 'paa-card-type-action',
    'ai-suggestion': 'paa-card-type-ai',
}


class PAACardWindow(Adw.Window):
    """Card-based PAA window showing actionable findings with chat panel."""

    def __init__(self, parent, ledger, settings, store=None, on_close=None, on_action=None):
        super().__init__()
        self._ledger = ledger
        self._settings = settings
        self._store = store
        self._on_close_cb = on_close
        self._on_action_cb = on_action
        self._closing = False
        self._child_pid = None
        self._spawn_cancelled = False
        self._discussing_item_id = None
        self._watches = []
        self._chat_harness_id = self._settings.effective_harness('') or 'claude'

        self.set_title('Projects Admin Agent')
        self.set_transient_for(parent)
        self.set_modal(False)

        pw = parent.get_width()
        ph = parent.get_height()
        self._base_width = min(700, int(pw * 0.8))
        self.set_default_size(self._base_width, int(ph * 0.85))

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key)
        self.add_controller(key_ctrl)
        self.connect('close-request', self._on_close_request)
        self.connect('destroy', self._on_destroy)

        # Layout
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        # Chat button in header
        chat_btn = Gtk.Button.new_from_icon_name('utilities-terminal-symbolic')
        chat_btn.set_tooltip_text('Open PAA chat')
        chat_btn.connect('clicked', self._on_chat_clicked)
        header.pack_end(chat_btn)

        toolbar.add_top_bar(header)

        # Paned: cards on left, terminal on right
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.add_css_class('paa-paned')

        # -- Left: card content --
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Stats row
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats.set_margin_start(16)
        stats.set_margin_end(16)
        stats.set_margin_top(12)
        stats.set_margin_bottom(8)
        self._pending_label = Gtk.Label()
        self._pending_label.add_css_class('paa-stats-count')
        stats.append(self._pending_label)
        self._scanning_label = Gtk.Label()
        self._scanning_label.add_css_class('paa-stats-count')
        self._scanning_label.set_hexpand(True)
        self._scanning_label.set_halign(Gtk.Align.CENTER)
        self._scanning_label.set_visible(False)
        stats.append(self._scanning_label)
        self._budget_label = Gtk.Label()
        self._budget_label.add_css_class('paa-stats-count')
        self._budget_label.set_hexpand(True)
        self._budget_label.set_halign(Gtk.Align.END)
        stats.append(self._budget_label)
        content.append(stats)

        # Always-visible chat/Discuss failure banner. The terminal pane can
        # stay at zero width on Wayland (set_default_size after present is a
        # no-op), so an error that only lands in VTE looks like "nothing
        # came up."
        self._error_bar = Gtk.Label(label='')
        self._error_bar.add_css_class('error')
        self._error_bar.set_wrap(True)
        self._error_bar.set_xalign(0)
        self._error_bar.set_margin_start(16)
        self._error_bar.set_margin_end(16)
        self._error_bar.set_margin_bottom(8)
        self._error_bar.set_visible(False)
        content.append(self._error_bar)

        # Health summary row
        self._health_label = Gtk.Label()
        self._health_label.add_css_class('paa-health-summary')
        self._health_label.set_halign(Gtk.Align.START)
        self._health_label.set_margin_start(16)
        self._health_label.set_margin_bottom(4)
        content.append(self._health_label)

        # Filter row
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filters.set_margin_start(16)
        filters.set_margin_end(16)
        filters.set_margin_bottom(8)

        self._project_names = ['All Projects']
        self._project_filter_model = Gtk.StringList.new(self._project_names)
        self._project_dropdown = Gtk.DropDown(model=self._project_filter_model)
        self._project_dropdown.set_selected(0)
        self._project_dropdown.add_css_class('flat')
        self._project_dropdown.connect('notify::selected', lambda *_: self._refresh())
        filters.append(self._project_dropdown)

        self._critical_btn = Gtk.ToggleButton(label='Critical')
        self._critical_btn.add_css_class('flat')
        self._critical_btn.connect('toggled', lambda *_: self._refresh())
        filters.append(self._critical_btn)

        self._type_names = ['All Types'] + list(_TYPE_LABELS.values())
        self._type_keys = [''] + list(_TYPE_LABELS.keys())
        self._type_filter_model = Gtk.StringList.new(self._type_names)
        self._type_dropdown = Gtk.DropDown(model=self._type_filter_model)
        self._type_dropdown.set_selected(0)
        self._type_dropdown.add_css_class('flat')
        self._type_dropdown.connect('notify::selected', lambda *_: self._refresh())
        filters.append(self._type_dropdown)

        self._show_ack_btn = Gtk.ToggleButton(label='Show acknowledged')
        self._show_ack_btn.add_css_class('flat')
        self._show_ack_btn.set_tooltip_text(
            'Include acknowledged (parked) items in the list.\n'
            'Off by default — toggle on to revisit them.'
        )
        self._show_ack_btn.connect('toggled', lambda *_: self._refresh())
        filters.append(self._show_ack_btn)

        content.append(filters)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(sep)

        # Scrollable card list
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._card_box.set_margin_start(8)
        self._card_box.set_margin_end(8)
        self._card_box.set_margin_top(8)
        self._card_box.set_margin_bottom(8)
        self._scrolled.set_child(self._card_box)
        content.append(self._scrolled)

        # Empty state
        self._empty = Adw.StatusPage()
        self._empty.set_vexpand(True)
        content.append(self._empty)

        self._paned.set_start_child(content)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)

        # -- Right: terminal panel (initially hidden) --
        self._terminal_panel = self._build_terminal_panel()
        self._terminal_panel.set_visible(False)
        self._paned.set_end_child(self._terminal_panel)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(False)

        toolbar.set_content(self._paned)
        self.set_content(toolbar)
        self._refresh()

    # ── Terminal panel ────────────────────────────────────────────────────

    def _build_terminal_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class('paa-terminal-panel')
        panel.set_size_request(400, -1)

        self._vte = Vte.Terminal()
        self._vte.set_hexpand(True)
        self._vte.set_vexpand(True)
        self._vte.set_scrollback_lines(self._settings.scrollback_lines)

        # Font
        desc = Pango.FontDescription.from_string(
            f'Monospace {self._settings.font_size}'
        )
        self._vte.set_font(desc)

        # Colors
        theme = getattr(self._settings, 'theme', 'argonaut')
        p = _TERMINAL_PALETTES.get(theme, _TERMINAL_PALETTES['argonaut'])
        def rgba(hex_str):
            c = Gdk.RGBA()
            c.parse(hex_str)
            return c
        self._vte.set_colors(
            rgba(p['fg']), rgba(p['bg']),
            [rgba(h) for h in p['palette']],
        )
        self._vte.set_color_cursor(rgba(p['cursor']))
        self._vte.set_color_cursor_foreground(rgba(p['cursor_fg']))

        self._vte.connect('child-exited', self._on_child_exited)

        # CAPTURE-phase VTE key remaps (resolved harness; updated on spawn).
        term_key_ctrl = Gtk.EventControllerKey.new()
        term_key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        term_key_ctrl.connect('key-pressed', self._on_terminal_key_pressed)
        self._vte.add_controller(term_key_ctrl)

        # Right-click context menu
        rclick = Gtk.GestureClick.new()
        rclick.set_button(3)
        rclick.connect('pressed', self._on_right_click)
        self._vte.add_controller(rclick)

        # Scrollbar
        scrollbar = Gtk.Scrollbar.new(
            Gtk.Orientation.VERTICAL, self._vte.get_vadjustment()
        )
        term_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        term_box.append(self._vte)
        term_box.append(scrollbar)
        term_box.set_vexpand(True)
        panel.append(term_box)

        # Status label
        self._chat_status = Gtk.Label(label='')
        self._chat_status.set_halign(Gtk.Align.START)
        self._chat_status.add_css_class('paa-chat-status')
        panel.append(self._chat_status)

        return panel

    # ── Harness deployment ────────────────────────────────────────────────

    def _deploy_harness(self):
        """Deploy PAA harness files to .project-admin-agent/ and return the path."""
        from paa_deploy import deploy_paa_harness
        src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'paa')
        return deploy_paa_harness(src_dir, self._settings.resolved_projects_dir)

    def _show_chat_error(self, err):
        """Surface a chat/Discuss failure on the cards side AND in VTE."""
        msg = str(err).strip() or 'unknown error'
        self._error_bar.set_label(f'PAA chat failed to start: {msg}')
        self._error_bar.set_visible(True)
        self._chat_status.set_label(f'Chat failed: {msg}')
        try:
            self._vte.feed(
                f'\r\nPAA chat failed to start.\r\n{msg}\r\n'.encode('utf-8')
            )
        except Exception:
            pass
        try:
            from debug_log import debug_log
            debug_log(self._settings, f'paa chat failed: {msg}')
        except Exception:
            pass

    def _clear_chat_error(self):
        self._error_bar.set_visible(False)
        self._error_bar.set_label('')

    # ── Chat / Discuss spawning ───────────────────────────────────────────

    def _spawn_chat(self, prompt):
        self._kill_child()
        self._vte.reset(True, True)
        self._clear_chat_error()
        try:
            paa_dir = self._deploy_harness()
            from paa_deploy import paa_interactive_plan, write_startup_prompt
            write_startup_prompt(paa_dir, prompt)
            ip = paa_interactive_plan(self._settings, paa_dir, prompt)
            self._chat_harness_id = ip.harness_id
            self._spawn_cancelled = False
            argv = list(ip.plan.argv)
            env = ip.plan.env
        except Exception as e:
            self._show_chat_error(e)
            return
        try:
            from debug_log import debug_log, format_argv_for_debug
            debug_log(
                self._settings,
                'session launch kind=discuss '
                f'harness={ip.harness_id} cwd={paa_dir} '
                f'argv={format_argv_for_debug(argv)}',
            )
        except Exception:
            pass
        if ip.plan.fallback_reason:
            try:
                from debug_log import debug_log
                debug_log(self._settings,
                          f'paa chat fallback: {ip.plan.fallback_reason}')
            except Exception:
                pass
        self._spawn_vte_child(argv, env, paa_dir)

    def _spawn_vte_child(self, argv, env, cwd):
        """DIY fork+exec — same pidfd contract as TerminalView._spawn.

        Vte.spawn_async leaks pidfds (see docs/pidfd-leak-investigation.md)
        and does not prepend harness PATH. GUI-launched PM often cannot
        resolve ``grok`` / ``kimi`` / ``opencode`` without that.
        """
        import harnesses
        argv_list = list(argv)
        if not argv_list:
            self._show_chat_error('no command to spawn')
            return
        try:
            pty = Vte.Pty.new_sync(Vte.PtyFlags(0), None)
        except GLib.Error as e:
            self._child_pid = None
            self._show_chat_error(e)
            return
        pty.set_size(
            self._vte.get_row_count(),
            self._vte.get_column_count(),
        )
        env_dict = dict(env) if env is not None else dict(os.environ)
        env_dict = harnesses.with_harness_path(env_dict)
        env_dict.setdefault('TERM', 'xterm-256color')
        env_dict.setdefault('COLORTERM', 'truecolor')
        env_dict.setdefault('DISABLE_AUTOUPDATER', '1')
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    os.chdir(cwd)
                except OSError:
                    pass
                pty.child_setup()
                os.execvpe(argv_list[0], argv_list, env_dict)
            except Exception:
                pass
            os._exit(127)
        self._vte.set_pty(pty)
        self._child_pid = pid
        if self._spawn_cancelled:
            self._kill_child()
            return
        self._add_pidfd_watch(pid)

    def _on_child_exited(self, terminal, status):
        self._child_pid = None

    def _kill_child(self):
        if self._child_pid is not None:
            pid = self._child_pid
            self._child_pid = None
            for p in (-pid, pid):
                try:
                    os.kill(p, signal.SIGHUP)
                except (ProcessLookupError, OSError):
                    pass
        else:
            self._spawn_cancelled = True

    def _add_pidfd_watch(self, pid):
        try:
            fd = os.pidfd_open(pid, 0)
        except (OSError, ProcessLookupError):
            self._reap(pid)
            GLib.idle_add(self._on_child_gone, pid)
            return
        source_id = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            fd,
            GLib.IOCondition.IN,
            self._on_pidfd_ready,
            pid,
        )
        self._watches.append({'pid': pid, 'fd': fd, 'source_id': source_id})

    def _on_pidfd_ready(self, fd, condition, pid):
        status = self._reap(pid)
        try:
            os.close(fd)
        except OSError:
            pass
        self._watches = [w for w in self._watches if w['fd'] != fd]
        self._on_child_gone(pid, status)
        return False

    def _reap(self, pid):
        try:
            r = os.waitpid(pid, os.WNOHANG)
            if r and r[0] == pid:
                return r[1]
        except (ChildProcessError, OSError):
            pass
        return 0

    def _on_child_gone(self, pid, status=0):
        if pid != self._child_pid:
            return False
        self._child_pid = None
        try:
            code = os.waitstatus_to_exitcode(status)
        except (ValueError, ChildProcessError):
            code = None
        if code in (126, 127):
            hid = self._chat_harness_id or 'harness'
            self._show_chat_error(f'{hid} failed to start (exit {code})')
        return False

    # ── Terminal panel reveal ─────────────────────────────────────────────

    def _reveal_terminal(self):
        if not self._terminal_panel.get_visible():
            self._terminal_panel.set_visible(True)
            new_width = max(1200, self._base_width + 500)
            self.set_default_size(new_width, self.get_height())
            GLib.idle_add(lambda: self._paned.set_position(
                int(self.get_width() * 0.4)
            ) or False)

    def _hide_terminal(self):
        self._kill_child()
        self._terminal_panel.set_visible(False)
        self.set_default_size(self._base_width, self.get_height())

    # ── Discuss / Chat actions ────────────────────────────────────────────

    def _on_discuss(self, item):
        # Toggle: if already discussing this item, fold closed
        if (self._terminal_panel.get_visible()
                and self._discussing_item_id == item.id):
            self._discussing_item_id = None
            self._hide_terminal()
            GLib.idle_add(self._refresh)
            return

        type_label = _TYPE_LABELS.get(item.type, item.type)
        prompt = discuss_finding_prompt(item, self._ledger.pending_items())
        self._discussing_item_id = item.id
        self._reveal_terminal()
        self._spawn_chat(prompt)
        self._chat_status.set_label(
            f'Discussing: {item.project} \u2014 {type_label}'
        )
        GLib.idle_add(self._refresh)
        GLib.idle_add(self._vte.grab_focus)

    def _on_chat_clicked(self, button):
        # Toggle: if terminal is visible, fold closed
        if self._terminal_panel.get_visible():
            self._discussing_item_id = None
            self._hide_terminal()
            GLib.idle_add(self._refresh)
            return
        self._discussing_item_id = None
        self._reveal_terminal()
        self._spawn_chat('WELCOME')
        self._chat_status.set_label('General chat')
        GLib.idle_add(self._refresh)
        GLib.idle_add(self._vte.grab_focus)

    # ── Terminal keyboard / context menu ──────────────────────────────────

    def _on_terminal_key_pressed(self, controller, keyval, keycode, state):
        captures = self._settings.vte_captures_for(self._chat_harness_id)
        feed = match_vte_key_capture(captures, int(keyval), int(state))
        if feed is not None:
            self._vte.feed_child(feed)
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._vte.copy_clipboard_format(Vte.Format.TEXT)
                return True
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._vte.paste_clipboard()
                return True
        return False

    def _on_right_click(self, gesture, n_press, x, y):
        popover = Gtk.Popover()
        popover.set_parent(self._vte)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect('closed', lambda p: p.unparent())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.add_css_class('term-context-menu')
        box.set_size_request(160, -1)

        def item(label, callback, sensitive=True):
            btn = Gtk.Button()
            lbl = Gtk.Label(label=label)
            lbl.set_halign(Gtk.Align.START)
            btn.set_child(lbl)
            btn.add_css_class('flat')
            btn.set_sensitive(sensitive)
            btn.set_halign(Gtk.Align.FILL)
            btn.connect('clicked', lambda _b, cb=callback: (cb(), popover.popdown()))
            return btn

        has_sel = self._vte.get_has_selection()
        box.append(item('Copy', lambda: self._vte.copy_clipboard_format(Vte.Format.TEXT), has_sel))
        box.append(item('Paste', self._vte.paste_clipboard))
        box.append(item('Select All', self._vte.select_all))

        popover.set_child(box)
        popover.popup()

    # ── Card UI ───────────────────────────────────────────────────────────

    def _update_budget_label(self):
        if self._settings.paa_allow_haiku:
            if self._settings.paa_budget_unlimited:
                budget_text = f'Budget: unlimited ({self._settings.paa_budget_used:,} tokens used)'
            else:
                pct = min(100, int(
                    self._settings.paa_budget_used
                    / max(1, self._settings.paa_budget_tokens) * 100
                ))
                budget_text = (
                    f'Budget: {self._settings.paa_budget_used:,} / '
                    f'{self._settings.paa_budget_tokens:,} tokens ({pct}%)'
                )
            self._budget_label.set_label(budget_text)
            self._budget_label.set_visible(True)
        else:
            self._budget_label.set_visible(False)

    def _update_health_summary(self):
        if self._store is None:
            self._health_label.set_visible(False)
            return
        projects = self._store.load_projects()
        total = len(projects)
        with_git = sum(1 for p in projects if os.path.isdir(os.path.join(p.path, '.git')))
        with_agents = sum(1 for p in projects if os.path.isfile(os.path.join(p.path, 'AGENTS.md')))
        self._health_label.set_label(
            f'{total} projects \u2022 {with_git} with git \u2022 {with_agents} with AGENTS.md'
        )
        self._health_label.set_visible(True)

    def _refresh(self):
        self._update_budget_label()
        self._update_health_summary()
        self._stale = []
        self._scrolled.set_visible(False)
        while True:
            child = self._card_box.get_first_child()
            if child is None:
                break
            self._stale.append(child)
            self._card_box.remove(child)
        GLib.timeout_add(200, self._drop_stale)

        pending = self._ledger.pending_items()
        acknowledged = self._ledger.acknowledged_items()
        show_ack = self._show_ack_btn.get_active()

        pending_total = len(pending)
        ack_total = len(acknowledged)
        if show_ack and ack_total:
            self._pending_label.set_label(
                f'{pending_total} pending '
                f'• {ack_total} acknowledged'
            )
        else:
            self._pending_label.set_label(
                f'{pending_total} pending item{"s" if pending_total != 1 else ""}'
            )

        # Update project dropdown — include acknowledged so the user can still
        # filter on a project that only has parked items in the toggled view.
        scope_for_dropdown = pending + (acknowledged if show_ack else [])
        projects_in_ledger = sorted({i.project for i in scope_for_dropdown})
        new_names = ['All Projects'] + projects_in_ledger
        if new_names != self._project_names:
            self._project_names = new_names
            sel = self._project_dropdown.get_selected()
            self._project_filter_model.splice(
                0, self._project_filter_model.get_n_items(), new_names
            )
            if sel < len(new_names):
                self._project_dropdown.set_selected(sel)

        # Apply filters separately so pending stays grouped above acknowledged.
        all_items = pending + (acknowledged if show_ack else [])
        items = all_items
        proj_idx = self._project_dropdown.get_selected()
        if proj_idx > 0 and proj_idx < len(self._project_names):
            proj_name = self._project_names[proj_idx]
            items = [i for i in items if i.project == proj_name]
        if self._critical_btn.get_active():
            items = [i for i in items if i.severity == 'critical']
        type_idx = self._type_dropdown.get_selected()
        if type_idx > 0 and type_idx < len(self._type_keys):
            type_key = self._type_keys[type_idx]
            items = [i for i in items if i.type == type_key]

        has_items = len(items) > 0
        self._scrolled.set_visible(has_items)
        self._empty.set_visible(not has_items)

        if not has_items:
            if not self._settings.paa_enabled:
                self._empty.set_title('PAA Disabled')
                self._empty.set_description(
                    'Enable it in Settings \u2192 PAA (Ctrl+comma)'
                )
                self._empty.set_icon_name('system-shutdown-symbolic')
            elif len(all_items) > 0:
                self._empty.set_title('No Matches')
                self._empty.set_description(
                    'No items match the current filters.'
                )
                self._empty.set_icon_name('edit-find-symbolic')
            else:
                self._empty.set_title('All Clear')
                self._empty.set_description(
                    'No issues found across your projects.'
                )
                self._empty.set_icon_name('emblem-ok-symbolic')
            return

        for item in items:
            self._card_box.append(self._build_card(item))

    def _build_card(self, item):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class('paa-card')
        if item.status == 'acknowledged':
            card.add_css_class('paa-card-acknowledged')
        if (self._terminal_panel.get_visible()
                and self._discussing_item_id == item.id):
            card.add_css_class('paa-card-discussing')

        # Header: card id + type badge + project name + critical badge
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        id_lbl = Gtk.Label(label=item.id[:6])
        id_lbl.add_css_class('paa-card-id')
        id_lbl.set_selectable(True)
        id_lbl.set_tooltip_text(f'Card ID: {item.id}')
        header.append(id_lbl)
        badge = Gtk.Label(label=_TYPE_LABELS.get(item.type, item.type))
        badge.add_css_class('paa-card-type')
        badge.add_css_class(_SEVERITY_CSS.get(item.severity, 'paa-card-type-info'))
        if item.type.startswith('xp-'):
            badge.add_css_class('paa-card-xp')
        header.append(badge)
        proj_lbl = Gtk.Label(label=item.project)
        proj_lbl.add_css_class('paa-card-project')
        proj_lbl.set_hexpand(True)
        proj_lbl.set_halign(Gtk.Align.START)
        header.append(proj_lbl)
        if item.severity == 'critical':
            crit_badge = Gtk.Label(label='CRITICAL')
            crit_badge.add_css_class('paa-card-type')
            crit_badge.add_css_class('paa-card-critical')
            header.append(crit_badge)
        card.append(header)

        # Summary
        summary = Gtk.Label(label=item.summary)
        summary.set_halign(Gtk.Align.START)
        summary.set_wrap(True)
        summary.set_xalign(0)
        summary.add_css_class('paa-card-summary')
        card.append(summary)

        # Evidence
        evidence = Gtk.Label(label=item.evidence)
        evidence.set_halign(Gtk.Align.START)
        evidence.set_wrap(True)
        evidence.set_xalign(0)
        evidence.add_css_class('paa-card-evidence')
        evidence.set_selectable(True)
        card.append(evidence)

        # Action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        actions.set_margin_top(4)

        dismiss_btn = Gtk.Button(label='Dismiss')
        dismiss_btn.add_css_class('flat')
        dismiss_btn.set_tooltip_text(
            'Not a problem \u2014 don\'t show this again.'
        )
        dismiss_btn.connect(
            'clicked', lambda b, iid=item.id: self._on_dismiss(iid)
        )
        actions.append(dismiss_btn)

        if item.status == 'acknowledged':
            ack_btn = Gtk.Button(label='Un-acknowledge')
            ack_btn.add_css_class('flat')
            ack_btn.set_tooltip_text(
                'Move this item back to pending.'
            )
            ack_btn.connect(
                'clicked', lambda b, iid=item.id: self._on_unacknowledge(iid)
            )
        else:
            ack_btn = Gtk.Button(label='Acknowledge')
            ack_btn.add_css_class('suggested-action')
            ack_btn.set_tooltip_text(
                'I see it and accept it for now \u2014 parks this card.\n'
                'Hidden by default; toggle "Show acknowledged" to revisit.\n'
                'Auto-clears if the issue is fixed elsewhere.'
            )
            ack_btn.connect(
                'clicked', lambda b, iid=item.id: self._on_acknowledge(iid)
            )
        actions.append(ack_btn)

        discuss_btn = Gtk.ToggleButton(label='Discuss')
        discuss_btn.add_css_class('paa-discuss-btn')
        discuss_btn.set_tooltip_text('Discuss this finding with the PAA agent')
        discuss_btn.set_active(
            self._terminal_panel.get_visible()
            and self._discussing_item_id == item.id
        )
        discuss_btn.connect('toggled', lambda b, i=item: self._on_discuss(i))
        actions.append(discuss_btn)

        card.append(actions)
        return card

    # ── Card actions ──────────────────────────────────────────────────────

    def _on_dismiss(self, item_id):
        if self._discussing_item_id == item_id:
            self._discussing_item_id = None
            self._hide_terminal()
        self._ledger.update_status(item_id, 'dismissed')
        self._ledger.save()
        if self._on_action_cb:
            self._on_action_cb(self._ledger.pending_count)
        GLib.idle_add(self._refresh)

    def _on_acknowledge(self, item_id):
        if self._discussing_item_id == item_id:
            self._discussing_item_id = None
            self._hide_terminal()
        self._ledger.update_status(item_id, 'acknowledged')
        self._ledger.save()
        if self._on_action_cb:
            self._on_action_cb(self._ledger.pending_count)
        GLib.idle_add(self._refresh)

    def _on_unacknowledge(self, item_id):
        self._ledger.update_status(item_id, 'pending')
        self._ledger.save()
        if self._on_action_cb:
            self._on_action_cb(self._ledger.pending_count)
        GLib.idle_add(self._refresh)

    def _drop_stale(self):
        self._stale = None
        return GLib.SOURCE_REMOVE

    def refresh_from_scan(self):
        """Called when the monitor completes a scan."""
        self._refresh()

    def set_scanning(self, names):
        """Show which projects are being AI-scanned, or hide when done."""
        if names:
            self._scanning_label.set_label(f'Scanning: {names}')
            self._scanning_label.set_visible(True)
        else:
            self._scanning_label.set_visible(False)

    # ── Window lifecycle ──────────────────────────────────────────────────

    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            if self._terminal_panel.get_visible() and self._vte.has_focus():
                self._hide_terminal()
                return True
            self.close()
            return True
        return False

    def _on_close_request(self, window):
        if self._closing:
            return True
        self._closing = True
        self._kill_child()
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
        GLib.idle_add(self.destroy)
        return True

    def _on_destroy(self, window):
        self._kill_child()
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
