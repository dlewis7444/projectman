import os
import re
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Pango, GObject, Gdk, Gio

import harnesses
import terminal_copy
import zellij


_TERMINAL_PALETTES = {
    'argonaut': {
        'fg': '#fffaf4', 'bg': '#0e1019', 'cursor': '#ff0018', 'cursor_fg': '#0e1019',
        'palette': [
            '#232323', '#ff000f', '#8ce10b', '#ffb900',
            '#008df8', '#6d43a6', '#00d8eb', '#ffffff',
            '#444444', '#ff2740', '#abe15b', '#ffd242',
            '#0092ff', '#9a5feb', '#67fff0', '#ffffff',
        ],
    },
    'candyland': {
        'fg': '#fce4f7', 'bg': '#1a0a1e', 'cursor': '#ff6eb4', 'cursor_fg': '#1a0a1e',
        'palette': [
            '#1a0a1e', '#ff5c8a', '#6ee7b7', '#ffcc66',
            '#7cacf8', '#c084fc', '#67e8f9', '#fce4f7',
            '#4a2d5e', '#ff8fab', '#a7f3d0', '#fde68a',
            '#a5b4fc', '#d8b4fe', '#a5f3fc', '#ffffff',
        ],
    },
    'phosphor': {
        'fg': '#33ff00', 'bg': '#060808', 'cursor': '#33ff00', 'cursor_fg': '#060808',
        'palette': [
            '#060808', '#1a7a00', '#33ff00', '#ffb300',
            '#00e5ff', '#1a7a3a', '#00cc88', '#33ff00',
            '#0d1a0d', '#22aa00', '#55ff33', '#ffc933',
            '#33eeff', '#44ff99', '#00ffcc', '#aaffaa',
        ],
    },
    'salt-spray': {
        'fg': '#90d5f0', 'bg': '#012a4a', 'cursor': '#00b4d8', 'cursor_fg': '#012a4a',
        'palette': [
            '#011a2e', '#e05555', '#1a9a6a', '#d4841a',  # 0-3: black, red, green, yellow
            '#0077b6', '#7a5aaa', '#00b4d8', '#90d5f0',  # 4-7: blue, mag, cyan, white
            '#1a4a7a', '#e74c3c', '#3aaed4', '#f4a124',  # 8-11: dim, bred, bgrn, byel
            '#48cae4', '#9a6ac8', '#90e0ef', '#cce8f6',  # 12-15: bblu, bmag, bcyn, bwht
        ],
    },
}


def _ensure_zellij_shell_wrapper():
    """Write (or overwrite) ~/.ProjectMan/zellij-shell-init.sh; return its path.

    This wrapper is set as SHELL when creating new zellij sessions so that the
    initial pane auto-starts the project's agent. It checks for a per-session
    flag file (~/.ProjectMan/.zellij-init-<session>) and, if present, reads its
    content (the harness's continue command line), removes the flag, execs the
    command, then exits (closing the pane). Subsequent panes in the same session
    find no flag and go straight to the real shell.

    The script is harness-independent (it execs whatever the flag holds); the
    per-harness command is written into the flag by ``spawn_zellij``. For claude
    the realized behavior is identical to the pre-seam hardcoded
    ``claude -c || claude`` — pinned by the golden tests.
    """
    pm_dir = os.path.expanduser('~/.ProjectMan')
    wrapper_path = os.path.join(pm_dir, 'zellij-shell-init.sh')
    with open(wrapper_path, 'w') as f:
        f.write(harnesses.build_zellij_wrapper_script())
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


class TerminalView(Gtk.Box):
    __gsignals__ = {
        'process-exited':   (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        'process-started':  (GObject.SignalFlags.RUN_FIRST, None, ()),
        'process-detached': (GObject.SignalFlags.RUN_FIRST, None, ()),
        # M-UX.10a (C7): a spawn that failed because the harness binary isn't
        # installed (child exited within ~2s with rc 126/127 — exec-not-found /
        # not-executable). Carries the missing binary name so window.py can fire
        # a one-shot install-hint toast and KEEP the row, instead of leaving a
        # raw bash error + a vanished row. process-exited still fires too (the
        # row goes 'inactive'); this is the explanatory layer on top.
        'process-spawn-failed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Fired at spawn entry (before kill) so window.py can cancel a pending
        # deferred deactivate before the grace timer can race a respawn.
        'spawn-begin': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, project, settings, harness_id=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._project = project
        self._settings = settings
        # The harness backend for this project (claude by default). All spawn
        # argv/env decisions route through the adapter; TerminalView keeps only
        # the pty/fork/watch machinery, which is already agent-agnostic.
        #
        # ``harness_id`` is the explicit agent for this terminal (A2/M2). When
        # given it OVERRIDES ``settings.effective_harness`` — this is how restore
        # recreates the harness that was actually running (saved-harness-wins),
        # even if settings now name a different default. When None, the
        # effective harness from settings is used (settings-wins on a new
        # activation). The resolved id is remembered so ``apply_settings`` keeps
        # honoring an explicit restore agent across settings reloads.
        self._explicit_harness = harness_id
        resolved = harness_id if harness_id is not None else settings.effective_harness(project.path)
        # Pass settings so a named-but-missing agent falls back per the M-P3.2
        # policy (harness_default → first-available) rather than hardcoding claude:
        # the Claude-less promise must hold even for a stale/bogus override.
        self._adapter = harnesses.get_adapter(resolved, settings)
        self._child_pid = None
        self._is_multiplexed = False
        self._is_zellij = False
        self._zellij_session = None
        # 'provider/model' the live child was spawned with ('' = native Claude);
        # lets window.py detect when a model change leaves a session stale.
        self._spawned_model = ''
        # Agent id the live child was spawned with (parallel to _spawned_model).
        self._spawned_harness = self._adapter.id
        # Set by the adapter's spawn plan / zellij_spawn_env when a custom-model
        # backend (custom provider) is wanted but unavailable; cleared on success.
        # window.py reads this from the process-started handler.
        self._fallback_reason = None
        # M-UX.10a: spawn-failure detection. Stamped at each _spawn with the
        # monotonic time + the binary argv[0]; consulted in the exit path to tell
        # an exec-not-found death (rc 126/127 within ~2s) from a normal exit.
        self._spawn_monotonic = None
        self._spawn_binary = None
        self._font_size = settings.font_size
        # PM-owned pidfd watches, one per spawned child. Each entry is a dict
        # {pid, fd, source_id}. Vte's reaper is no longer in the picture —
        # _spawn does its own fork+exec and never calls watch_child. See
        # docs/pidfd-leak-investigation.md.
        self._watches = []

        self._terminal = Vte.Terminal()
        self._terminal.set_scrollback_lines(settings.scrollback_lines)
        self._terminal.set_audible_bell(settings.audible_bell)
        self._terminal.set_bold_is_bright(True)
        self._terminal.set_hexpand(True)
        self._terminal.set_vexpand(True)
        self._terminal.connect('child-exited', self._on_child_exited)
        self._apply_font()
        self._apply_colors()

        # URL/path matching — opens links on click
        # PCRE2_MULTILINE (0x400) required by VTE's match_add_regex
        url_regex = Vte.Regex.new_for_match(
            r'https?://\S+|file://\S+', -1, 0x400
        )
        self._url_tag = self._terminal.match_add_regex(url_regex, 0)
        self._terminal.match_set_cursor_name(self._url_tag, 'pointer')

        # Plain absolute paths — converted to file:// on click
        path_regex = Vte.Regex.new_for_match(r'/[^\s]+', -1, 0x400)
        self._path_tag = self._terminal.match_add_regex(path_regex, 0)
        self._terminal.match_set_cursor_name(self._path_tag, 'pointer')

        scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=self._terminal.get_vadjustment(),
        )
        term_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        term_box.set_hexpand(True)
        term_box.set_vexpand(True)
        term_box.append(self._terminal)
        term_box.append(scrollbar)
        self.append(term_box)

        # Intercept Shift+Enter at CAPTURE phase — GTK4/Wayland strips the Shift
        # modifier before VTE sees it; feed kitty keyboard protocol sequence directly.
        self._key_ctrl = Gtk.EventControllerKey.new()
        self._key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._key_ctrl.connect('key-pressed', self._on_key_pressed)
        self._terminal.add_controller(self._key_ctrl)

        # Ctrl+click opens URLs/paths — VTE doesn't claim modified clicks
        self._click_gesture = Gtk.GestureClick.new()
        self._click_gesture.set_button(1)
        self._click_gesture.connect('pressed', self._on_ctrl_click)
        self._terminal.add_controller(self._click_gesture)

        # Right-click context menu
        self._rclick_gesture = Gtk.GestureClick.new()
        self._rclick_gesture.set_button(3)
        self._rclick_gesture.connect('pressed', self._on_right_click)
        self._terminal.add_controller(self._rclick_gesture)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._terminal.feed_child(b'\x1b[13;2u')
                return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._smart_copy()
                return True
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
                self._terminal.paste_clipboard()
                return True
        return False

    def _debug(self, msg):
        if self._settings.debug_logging:
            print(f'[DBG] {msg}', flush=True)

    def _on_ctrl_click(self, gesture, n_press, x, y):
        # GestureClick.get_current_event_state() and get_last_event() both
        # drop modifiers on Wayland/GTK4 for mouse clicks. Query the live
        # keyboard modifier state from the seat instead.
        state = 0
        display = Gdk.Display.get_default()
        if display is not None:
            seat = display.get_default_seat()
            if seat is not None:
                keyboard = seat.get_keyboard()
                if keyboard is not None:
                    state = keyboard.get_modifier_state()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        self._debug(f'click n_press={n_press} x={x:.1f} y={y:.1f} ctrl={ctrl} state={int(state)}')
        if not ctrl:
            return
        self._open_url_at_coords(x, y)

    def _url_at(self, x, y):
        """Return URL/path string at pixel coords (x, y), or None.

        Uses VTE's check_match_at() so the regexes registered earlier with
        match_add_regex() do the work — including all the buffer/viewport
        coordinate translation and scrollback awareness that the previous
        manual implementation got wrong (it indexed scrollback rows with
        viewport row numbers, so any active scrollback turned every click
        into a "no match").
        """
        try:
            match, _tag = self._terminal.check_match_at(x, y)
        except Exception:
            return None
        if not match:
            return None
        # The registered \S+ regex greedily eats trailing punctuation.
        return re.sub(r'[)\].,;!?\'"]+$', '', match)

    def _open_url_at_coords(self, x, y):
        url = self._url_at(x, y)
        if not url:
            self._debug(f'no match at ({x:.0f}, {y:.0f})')
            return False
        uri = ('file://' + url) if url.startswith('/') else url
        self._debug(f'launching {uri}')
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return True

    def _on_right_click(self, gesture, n_press, x, y):
        url = self._url_at(x, y)
        self._show_context_menu(int(x), int(y), url)

    def _show_context_menu(self, x, y, url):
        popover = Gtk.Popover()
        popover.set_parent(self._terminal)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, y, 1, 1
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

        has_sel = self._terminal.get_has_selection()
        box.append(item('Copy', self._smart_copy, has_sel))
        box.append(item('Paste', self._terminal.paste_clipboard))
        box.append(item('Select All', self._terminal.select_all))

        if url:
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            uri = ('file://' + url) if url.startswith('/') else url
            box.append(item('Open URL', lambda u=uri: Gio.AppInfo.launch_default_for_uri(u, None)))
            box.append(item('Copy URL', lambda u=url: self._set_clipboard(u)))

        popover.set_child(box)
        popover.popup()

    def _set_clipboard(self, text):
        """Put ``text`` on the system clipboard.

        ``Gdk.Clipboard.set()`` propagates to other apps while this surface
        holds the keyboard focus — which it does during a right-click in the
        terminal. (An earlier revision shelled out to ``wl-copy`` to dodge a
        focus edge case, but wl-copy pops its own window and didn't reliably
        take the clipboard from an already-focused app — strictly worse;
        reverted 2026-06-26.) Never raises — a failed clipboard copy must
        not kill the context menu.
        """
        try:
            Gdk.Display.get_default().get_clipboard().set(text)
        except Exception as e:
            self._debug(f'clipboard set failed: {e!r}')

    def _smart_copy(self):
        """Copy selection with TUI-emitted hard-wrap artifacts collapsed.

        The selection from VTE already has `\\n  ` / `\\n   ` patterns where
        the inner program (Claude Code etc.) hard-wrapped long paragraphs at
        a 2-space hanging indent. `collapse_hard_wraps` rejoins those visual
        wraps back into flowing prose, leaving paragraph breaks, code
        indents, and short structural lines alone.

        Falls back to VTE's native copy on any error — worst case is today's
        pre-fix behavior.
        """
        try:
            if not self._terminal.get_has_selection():
                return
            baseline = self._terminal.get_text_selected(Vte.Format.TEXT)
            if not baseline:
                self._terminal.copy_clipboard_format(Vte.Format.TEXT)
                return
            result = terminal_copy.collapse_hard_wraps(baseline)
            self._debug(
                f'smart-copy baseline_len={len(baseline)} result_len={len(result)} '
                f'head={result[:80]!r}'
            )
            self._set_clipboard(result)
        except Exception as e:
            self._debug(f'smart-copy failed: {e!r}; falling back to native copy')
            try:
                self._terminal.copy_clipboard_format(Vte.Format.TEXT)
            except Exception:
                pass

    def _apply_font(self):
        desc = Pango.FontDescription.from_string(f'Monospace {self._font_size}')
        self._terminal.set_font(desc)

    def _apply_colors(self):
        def rgba(hex_str):
            c = Gdk.RGBA()
            c.parse(hex_str)
            return c

        theme = getattr(self._settings, 'theme', 'argonaut')
        p = _TERMINAL_PALETTES.get(theme, _TERMINAL_PALETTES['argonaut'])
        fg = rgba(p['fg'])
        bg = rgba(p['bg'])
        palette = [rgba(h) for h in p['palette']]
        self._terminal.set_colors(fg, bg, palette)
        self._terminal.set_color_cursor(rgba(p['cursor']))
        self._terminal.set_color_cursor_foreground(rgba(p['cursor_fg']))

    def spawned_model_signature(self):
        """The 'provider/model' the current child was spawned with."""
        return self._spawned_model

    def spawned_harness_signature(self):
        """The harness id the current child was spawned with."""
        return self._spawned_harness

    def spawn_harness(self, mode, session_id=None):
        """Spawn the project's agent in ``mode`` (fresh|continue|resume).

        The adapter folds argv + env into a ``SpawnPlan``; this
        method owns only the terminal-side lifecycle (clear, kill, spawn) and
        carries the plan's fallback_reason onto the instance so window.py can
        surface it from the process-started handler.
        """
        # Must clear at spawn entry, not in _spawn() — the adapter's spawn_plan
        # (which runs build_spawn_env for claude) sets a fresh reason below; clearing here
        # avoids a stale reason from a prior failed spawn leaking through.
        self._fallback_reason = None
        self.emit('spawn-begin')
        # FB-4 (C4/C6): a harness-change / new-session DIRECT spawn over a live
        # zellij project must tear down the zellij SERVER session first — the
        # deactivate path already does this; the spawn path used to kill only the
        # local attach child (_kill_child), orphaning the server. Mirror the
        # deactivate kill via the shared zellij.kill_session helper. Only when
        # this terminal currently holds a zellij session; a non-zellij spawn skips
        # it. Clear the zellij flags so the new direct child isn't mistaken for a
        # detach on its eventual exit.
        if self._is_zellij and self._zellij_session:
            zellij.kill_session(self._zellij_session)
            self._is_zellij = False
            self._zellij_session = None
        self._kill_child()
        self._terminal.reset(True, True)
        # FB-9: re-resolve the adapter at spawn entry from the CURRENT effective
        # state — an explicit restore agent when one is still set (A2 sticky),
        # else settings.effective_harness. After a true session end cleared
        # _explicit_harness, this is what lets the NEXT spawn follow a pending
        # per-project override (the deactivate→reactivate repro) instead of the
        # dead session's agent. A restored session that was never ended keeps its
        # explicit agent, so A2 is intact.
        host_id = getattr(self._project, 'host_id', None) or 'localhost'
        resolved = (self._explicit_harness if self._explicit_harness is not None
                    else self._settings.effective_harness(
                        self._project.path, host_id=host_id))
        self._adapter = harnesses.get_adapter(resolved, self._settings)
        plan = self._adapter.spawn_plan(
            self._settings, self._project, mode, session_id=session_id
        )
        self._fallback_reason = plan.fallback_reason
        self._is_multiplexed = False
        argv, env = self._maybe_wrap_ssh(plan.argv, plan.env, host_id)
        if argv is None:
            # _maybe_wrap_ssh set _fallback_reason
            return
        self._spawn(argv, env)

    def _maybe_wrap_ssh(self, argv, env, host_id):
        """If project is remote, rewrite into ``ssh -tt … bash -lc …``.

        Returns ``(argv, env)`` for local ``_spawn``. Local env is None (inherit)
        so only the remote command carries provider env. Returns ``(None, None)``
        on hard failure (unknown host).
        """
        from hosts import LOCALHOST_ID
        if not host_id or host_id == LOCALHOST_ID:
            return argv, env
        profiles = self._settings.host_profiles()
        prof = profiles.get(host_id)
        if prof is None:
            self._fallback_reason = f'Unknown remote host {host_id!r}'
            return None, None
        # Rebuild argv with the host's binary (never a local absolute path).
        remote_bin = prof.binary_spec(self._adapter.id).resolved(
            path_fallback=self._adapter.id,
        )
        import harnesses as _h
        fallback = self._adapter.caps.continue_falls_back_to_fresh
        # Infer mode from plan.argv shape is fragile; re-derive from last plan
        # by re-calling adapter helpers when available.
        mode_argv = list(argv)
        if hasattr(self._adapter, 'fresh_argv'):
            # Replace binary tokens in common shapes: [bin], [bin, -c], bash wrapper.
            # Prefer rebuild from mode stored on instance if we set it; else
            # rewrite argv[0] when it is a direct binary spawn.
            pass
        # Rebuild from the same mode as spawn_harness by inspecting argv:
        # bash -c continue wrapper → continue; else if --resume → resume; else fresh.
        # Grok: pin --cwd to the project directory so hooks write status under
        # the project path (not $HOME). We already cd there; '.' is that dir.
        grok_cwd = (['--cwd', '.'] if self._adapter.id == 'grok' else [])
        rebuilt = None
        if mode_argv and mode_argv[0] == 'bash' and len(mode_argv) >= 3:
            rebuilt = _h.build_continue_wrapper(
                [remote_bin, *grok_cwd, '-c'],
                [remote_bin, *grok_cwd],
                fallback=fallback,
            )
        elif len(mode_argv) >= 3 and mode_argv[1] == '--resume':
            rebuilt = [remote_bin, *grok_cwd, '--resume', mode_argv[2]]
        elif len(mode_argv) >= 2 and mode_argv[1] == '-c':
            rebuilt = _h.build_continue_wrapper(
                [remote_bin, *grok_cwd, '-c'],
                [remote_bin, *grok_cwd],
                fallback=fallback,
            )
        else:
            rebuilt = [remote_bin, *grok_cwd]
        # Provider env on the *remote* process only — never the full laptop
        # environ (HOME/USER would rewrite remote paths to the workstation user).
        from ssh_transport import build_ssh_spawn_argv, filter_remote_export_env
        remote_cwd = getattr(self._project, 'spawn_cwd', None) or self._project.path
        ssh_argv = build_ssh_spawn_argv(
            prof.ssh_target,
            remote_cwd,
            rebuilt,
            filter_remote_export_env(env),
            batch=False,  # interactive: allow prompts if any
            connect_timeout=15,
        )
        return ssh_argv, None

    def spawn_continue(self, project_name=None):
        """Continue the harness's most recent conversation (``-c`` semantics)."""
        self.spawn_harness('continue')

    def spawn_fresh(self, project_name=None):
        """Start a brand-new harness session."""
        self.spawn_harness('fresh')

    def spawn_resume(self, session_id, project_name=None):
        """Resume a specific session by id."""
        self.spawn_harness('resume', session_id=session_id)

    def spawn_claude(self, session_id=None, fresh=False, project_name=None):
        """Deprecated harness-neutral alias over ``spawn_harness`` (the name is a
        historical artifact — it spawns whatever the project's adapter is, not
        necessarily claude). Retained so the P1 consumption-seam tests keep
        passing; window.py now calls ``spawn_continue``/``spawn_fresh``/
        ``spawn_resume`` directly. Maps the old (session_id, fresh) flags onto a
        spawn mode."""
        if session_id:
            mode = 'resume'
        elif fresh:
            mode = 'fresh'
        else:
            mode = 'continue'
        self.spawn_harness(mode, session_id=session_id)

    def spawn_zellij(self, session_name):
        """Attach to or create a zellij session for this project.

        New sessions: created with a shell wrapper that auto-launches `claude -c`
        in the initial pane, then drops to the real shell.
        Existing sessions: attached with `zellij attach <name>`.
        """
        # Must clear at spawn entry — the attach branch never consults the
        # adapter's zellij_spawn_env(), so a stale reason from a prior failed
        # custom-model spawn would otherwise raise a spurious toast on a healthy
        # attach.
        self._fallback_reason = None
        self.emit('spawn-begin')
        self._kill_child()
        self._terminal.reset(True, True)
        self._is_zellij = True
        self._zellij_session = session_name
        self._is_multiplexed = True
        alive = zellij.session_alive(session_name)
        if alive:
            cmd = ['zellij', 'attach', session_name]
            env = None
        else:
            socket_path = os.path.join(zellij.socket_dir(), session_name)
            try:
                os.unlink(socket_path)
            except OSError:
                pass
            # Create per-session init flag CONTAINING the harness's continue
            # command line; the wrapper reads+execs it (generalized from the
            # old hardcoded `claude -c || claude`). For claude the content is
            # exactly `claude -c || claude` — byte-identical to before.
            flag_path = os.path.join(
                os.path.expanduser('~/.ProjectMan'), f'.zellij-init-{session_name}'
            )
            with open(flag_path, 'w') as f:
                f.write(self._adapter.zellij_continue_command(
                    self._settings, self._project))
            wrapper = _ensure_zellij_shell_wrapper()
            # Custom-model env can only be set when the zellij server is first
            # created — attaching to an existing session inherits whatever the
            # server already has. Per-project models under zellij are therefore
            # best-effort; the default (non-multiplexed) path is fully supported.
            # The adapter owns the env decision (A3/M3): for claude this is the
            # provider env; for opencode/grok (model-as-argv) it is None.
            # terminal.py never names ANTHROPIC_* keys itself.
            custom, self._fallback_reason = self._adapter.zellij_spawn_env(
                self._settings, self._project
            )
            env = dict(custom) if custom is not None else dict(os.environ)
            env['SHELL'] = wrapper
            env['ZELLIJ_REAL_SHELL'] = os.environ.get('SHELL', '/bin/bash')
            cmd = ['zellij', 'attach', '--create', session_name]
        self._spawn(cmd, env)

    def deactivate(self):
        """Gracefully stop the child; terminal output is preserved for context."""
        if self._child_pid is not None:
            for pid in (-self._child_pid, self._child_pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
            # child-exited signal will fire and emit process-exited

    def _spawn(self, argv, env=None):
        """DIY fork + exec — PM owns the child watch end-to-end.

        Replaces Vte.Terminal.spawn_async, which routes through vte's
        G_PRIORITY_LOW glib reaper. That reaper has been observed to leak
        pidfds (Pid: -1 fds accumulate, the main-loop poll() returns
        instantly forever, CPU pegs) and to miss child-exited dispatch
        (tab stuck "attached"). See docs/pidfd-leak-investigation.md.

        We create the pty via Vte.Pty.new_sync (cheap, no thread), fork in
        Python, let vte's own Pty.child_setup do the controlling-tty dance
        on the child side, then execvp. On the parent side we set the pty
        on the terminal (no watch_child call), open our own pidfd, and
        register a unix-fd watch at G_PRIORITY_DEFAULT. We never call
        vte_terminal_watch_child, so vte's reaper never gets a pidfd and
        can't leak one.
        """
        try:
            pty = Vte.Pty.new_sync(Vte.PtyFlags(0), None)
        except GLib.Error:
            self._child_pid = None
            return
        pty.set_size(
            self._terminal.get_row_count(),
            self._terminal.get_column_count(),
        )

        working_dir = self._project.path
        argv_list = list(argv)
        # Vte.Terminal.spawn_async used to inject TERM/COLORTERM into the child
        # env for us; the DIY fork+exec path (pty.child_setup only touches the
        # controlling tty) does not. Launched from a desktop launcher, PM has
        # no TERM of its own, so claude would render without color. setdefault
        # leaves a real inherited TERM alone.
        env_dict = dict(env) if env is not None else dict(os.environ)
        env_dict.setdefault('TERM', 'xterm-256color')
        env_dict.setdefault('COLORTERM', 'truecolor')
        # Freeze agent auto-updates for the session (Claude-focused; harmless
        # for harnesses that ignore it).
        env_dict.setdefault('DISABLE_AUTOUPDATER', '1')

        pid = os.fork()
        if pid == 0:
            # CHILD. Use only async-signal-safe / pre-exec-safe operations.
            try:
                try:
                    os.chdir(working_dir)
                except OSError:
                    pass
                # vte handles setsid, TIOCSCTTY, dup2(peer, 0/1/2), reset signals
                pty.child_setup()
                os.execvpe(argv_list[0], argv_list, env_dict)
            except Exception:
                pass
            os._exit(127)

        # PARENT.
        self._terminal.set_pty(pty)
        self._child_pid = pid
        # M-UX.10a: remember when + what we launched so the exit path can detect
        # an exec-not-found death. argv[0] is the binary; under the bash continue
        # wrapper (`bash -c …`) argv[0] is bash and the REAL miss surfaces as the
        # wrapper exiting 127 — still within the window, still attributable to the
        # adapter's binary, which window.py resolves from the adapter.
        import time as _time
        self._spawn_monotonic = _time.monotonic()
        self._spawn_binary = argv_list[0] if argv_list else ''
        self._spawned_model = self._settings.model_axis_signature(
            self._project.path)
        # The harness id the live child was actually spawned with — lets window.py
        # detect when a harness override leaves a running session stale (B3),
        # parallel to _spawned_model. Uses the adapter that produced this spawn.
        self._spawned_harness = self._adapter.id
        self._add_pidfd_watch(pid)
        self.emit('process-started')

    def _on_child_exited(self, terminal, status):
        """Vte's signal — never fires in our DIY-spawn path (we don't call
        watch_child). Left connected defensively in case some other code
        path ever does, so we don't lose the signal."""
        self._fire_exit_if_current(self._child_pid, status)

    def _add_pidfd_watch(self, pid):
        try:
            fd = os.pidfd_open(pid, 0)
        except (OSError, ProcessLookupError):
            # Child exited between spawn_finish and pidfd_open. Reap and
            # synthesize the exit on the next main-loop iteration.
            self._reap(pid)
            GLib.idle_add(self._fire_exit_if_current, pid, 0)
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
        self._fire_exit_if_current(pid, status)
        return False  # one-shot — remove the source

    def _reap(self, pid):
        try:
            r = os.waitpid(pid, os.WNOHANG)
            if r and r[0] == pid:
                return r[1]
        except (ChildProcessError, OSError):
            pass
        return 0

    def _fire_exit_if_current(self, pid, status):
        """Emit process-exited only if this pid is the one we're tracking.

        Stale exits (e.g. an old child dying after the user already restarted
        the session) are silently dropped — their pidfd has been cleaned up,
        and the UI has already moved on.
        """
        if pid != self._child_pid:
            return False
        self._child_pid = None
        # M-UX.10a: a fast exec-not-found death is a spawn FAILURE, not a normal
        # exit. Detect it BEFORE the zellij/exited emits so window.py can fire the
        # install-hint toast; process-exited still follows (row → 'inactive', kept).
        spawn_failed = self._is_spawn_failure(status)
        if self._is_zellij and self._zellij_session:
            if zellij.session_alive(self._zellij_session):
                # DETACH, not an end: the zellij session lives on and a reattach
                # legitimately resumes THIS agent. PRESERVE _explicit_harness — the
                # restored saved-agent (A2) must survive a detach/reattach.
                self.emit('process-detached')
                return False
            self._is_zellij = False
            self._zellij_session = None
        # FB-9 (the maintainer's reveal #2, C8-amended): sticky-agent lifetime = SESSION
        # lifetime. We are past the detach early-return, so the child has TRULY
        # ended (natural exit, deactivate via SIGTERM, zellij-kill, or spawn
        # failure) — not detached. Drop the construction-time restore agent so a
        # PENDING per-project override (e.g. restored-grok with the row now set
        # to claude) is honored on the next activation, instead of the dead
        # session's agent outliving it. The next spawn re-resolves the adapter
        # (spawn_harness), so a cleared explicit agent picks up effective_harness.
        self._explicit_harness = None
        if spawn_failed:
            self.emit('process-spawn-failed', self._spawn_binary or '')
        self.emit('process-exited', status)
        return False  # for idle_add — single-shot

    def _is_spawn_failure(self, status):
        """True when the just-exited child looks like an exec-not-found death.

        The signature (M-UX.10a): the child exited within ~2s of spawn with exit
        code 126 (found but not executable) or 127 (not found) — the codes
        ``os.execvpe`` failure produces (our fork child does ``os._exit(127)``;
        the bash continue-wrapper relays 127 from a failed ``exec``). A clean
        agent quit (rc 0) or a signal death never matches, so a real session that
        ends quickly is not mistaken for a missing binary. Pure + defensive.
        """
        if self._spawn_monotonic is None:
            return False
        import time as _time
        if (_time.monotonic() - self._spawn_monotonic) > 2.0:
            return False
        try:
            code = os.waitstatus_to_exitcode(status)
        except (ValueError, ChildProcessError):
            return False
        return code in (126, 127)

    def check_child_alive(self):
        """Defensive backup for our pidfd watch.

        Our pidfd watch (registered in _add_pidfd_watch) is the primary
        mechanism for detecting child exit. This poll is kept as a belt-and-
        suspenders catch in case the watch ever misses (it shouldn't — we
        register at G_PRIORITY_DEFAULT, not vte's G_PRIORITY_LOW, and we own
        the pidfd lifecycle ourselves). If we detect a gone/zombied child,
        reap and synthesize the exit so the UI catches up.
        """
        pid = self._child_pid
        if pid is None:
            return
        try:
            with open(f'/proc/{pid}/status') as f:
                state = next(
                    (line.split()[1] for line in f if line.startswith('State:')),
                    None,
                )
        except FileNotFoundError:
            state = 'gone'
        except OSError:
            return
        if state not in ('Z', 'gone'):
            return
        status = self._reap(pid)
        # Guard against a racing respawn: only fire if the PID hasn't changed.
        self._fire_exit_if_current(pid, status)

    def _kill_child(self):
        if self._child_pid is not None:
            for pid in (-self._child_pid, self._child_pid):
                try:
                    os.kill(pid, signal.SIGHUP)
                except (ProcessLookupError, OSError):
                    pass
            self._child_pid = None
            self._terminal.reset(True, True)

    def zoom_in(self):
        self._font_size = min(self._font_size + 1, 36)
        self._apply_font()

    def zoom_out(self):
        self._font_size = max(self._font_size - 1, 6)
        self._apply_font()

    def zoom_reset(self):
        self._font_size = self._settings.font_size
        self._apply_font()

    def apply_settings(self, settings):
        # Note: resets font_size to the settings default, discarding any active zoom offset.
        self._settings = settings
        # Re-resolve the adapter in case the effective harness changed. The next
        # spawn uses it; a live child keeps running its original agent. An
        # explicit construction-time agent (a restored session, A2) is sticky:
        # settings changes must not silently swap a running restored agent.
        resolved = (self._explicit_harness if self._explicit_harness is not None
                    else settings.effective_harness(self._project.path))
        self._adapter = harnesses.get_adapter(resolved, settings)
        self._font_size = settings.font_size
        self._apply_font()
        self._apply_colors()
        self._terminal.set_scrollback_lines(settings.scrollback_lines)
        self._terminal.set_audible_bell(settings.audible_bell)

    def clear_explicit_harness(self):
        """Drop the sticky restore agent so an EXPLICIT per-project pick wins.

        Contract (S8 ship-blocker): ``_explicit_harness`` makes a RESTORED
        session's agent sticky (A2) so an *incidental* GLOBAL/default settings
        change can't silently swap a running restored agent. But a deliberate
        per-project Agent-submenu pick (B3) MUST defeat that stickiness —
        otherwise the restart prompt re-spawns the OLD agent. Clearing the
        override lets the next ``apply_settings`` re-resolve the adapter to the
        new ``effective_harness``. ONLY the explicit-pick handler calls this;
        global/default changes never do, so A2 stays intact.
        """
        self._explicit_harness = None

    def feed_session_ended(self, code):
        """FB-3 (C7): write a one-line 'session ended' banner into the pane —
        DISPLAY ONLY (vte feed_child-free: this is ``feed``, terminal output, not
        input to a child). A late-dying child leaves a frozen pane with no
        explanation; this line says it ended and with what exit code. Never
        raises (a feed on a torn-down terminal must not crash the exit path)."""
        try:
            self._terminal.feed(
                f'\r\n[session ended — exit {code}]\r\n'.encode('utf-8'))
        except Exception:
            pass

    def get_terminal(self):
        return self._terminal
