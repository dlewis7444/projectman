"""Harness seam — ProjectMan's spawn contract for the coding agent.

Claude Code is the sole harness (the 2026-06 pivot collapsed the multi-harness
experiment; the agnostic axis that mattered was the MODEL, now a first-class
layer in ``settings.py`` + ``models.py``). This module is the spawn + sessions
seam: a ``ClaudeAdapter`` declares its capabilities and turns a
``(settings, project, mode)`` request into a concrete ``SpawnPlan`` (argv + env)
that ``terminal.py`` executes below the line, unchanged.

The env half (custom-provider model injection) is delegated to
``models.build_spawn_env``; the adapter stays the single place the spawn path
consults for env. The module is pure (no GTK, like ``zellij.py``/``session.py``)
so it is fully unit-testable headless; the golden tests in
``tests/test_agent_seam.py`` pin the adapter to the spawn argv/env/zellij
strings.
"""

import shlex
from dataclasses import dataclass


@dataclass
class AgentCaps:
    """What the harness supports. The UI degrades feature-by-feature on these
    flags rather than gating a whole harness."""
    continue_: bool = False     # resume the most recent conversation
    resume_by_id: bool = False  # resume a specific session id
    sessions: bool = False      # per-project session enumeration (history expander)
    rich_status: bool = False   # lifecycle events → live status dots
    model_select: bool = False  # per-project model is meaningful
    headless_json: bool = False # `-p`-style structured output (PAA-relevant)
    # Continue-fallback policy: when continue (`-c`) finds nothing to resume,
    # does the wrapper fall back to a fresh session? claude exits 1 cleanly on
    # nothing-to-continue, so it keeps the fallback (True).
    continue_falls_back_to_fresh: bool = True


@dataclass
class SessionRef:
    """A restorable past session. ``id`` is opaque and adapter-interpreted."""
    id: str
    title: str
    last_active: int


@dataclass
class SpawnPlan:
    """A concrete spawn request: argv + optional env override + fallback note.

    ``env`` is ``None`` to inherit the parent environment unchanged (the native
    path); a dict to override it (e.g. custom-provider model injection). A dict
    overrides the parent env rather than replacing it — see build_spawn_env.
    ``fallback_reason`` is a human-readable string when the adapter wanted a
    richer spawn but degraded (e.g. custom provider unavailable → native), for
    the UI to surface; ``None`` otherwise.
    """
    argv: list
    env: dict | None = None
    fallback_reason: str | None = None


# ---------------------------------------------------------------------------
# Spawn-wrapper builders (pure string/argv construction).
# ---------------------------------------------------------------------------

def build_continue_wrapper(continue_argv, fresh_argv, *, fallback=True):
    """Wrap a continue-then-fresh fallback in a bash trap/exit-code guard.

    It runs ``continue_argv`` (e.g. ``claude -c``); if that exits non-zero for a
    *non-signal* reason (claude -c exits 1 when there is no history to resume),
    it ``exec``s ``fresh_argv`` (e.g. ``claude``). Two guards keep PM's
    SIGTERM/SIGHUP from accidentally respawning a fresh agent:

      * the exit-code test rejects signal kills (status > 128);
      * the ``trap 'exit 143' TERM HUP`` rejects the graceful-but-nonzero case
        where the agent catches the signal and cleanly returns 1-128. Without
        the trap that case slips past the exit-code test and bash exec's a fresh
        agent that never saw the signal, leaving the project stuck "active".

    ``fallback`` is the continue-fallback policy: when False the wrapper runs the
    continue command alone (still under the signal trap) and NEVER exec's fresh.

    For claude's argvs (``continue_argv=[bin, '-c']``, ``fresh_argv=[bin]``) with
    the default ``fallback=True`` the returned list is byte-identical to the
    pre-refactor wrapper — pinned by the golden test. Returns a
    ``['bash', '-c', <script>]`` argv list.
    """
    cont = shlex.join(continue_argv)
    if not fallback:
        return ['bash', '-c', f"trap 'exit 143' TERM HUP; exec {cont}"]
    fresh = shlex.join(fresh_argv)
    return ['bash', '-c',
            f"trap 'exit 143' TERM HUP; {cont}; s=$?; "
            f'[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec {fresh}']


def build_zellij_continue_command(continue_argv, fresh_argv, *, fallback=True):
    """Shell command line the zellij init pane runs to continue-or-start.

    The per-session flag file CONTAINS this string and the wrapper execs it.
    For claude's argvs with the default ``fallback=True`` the result is
    ``'claude -c || claude'`` — byte-identical to today (golden-pinned).
    """
    cont = shlex.join(continue_argv)
    if not fallback:
        return cont
    return f'{cont} || {shlex.join(fresh_argv)}'


# Generalized zellij shell wrapper. It reads the per-session flag file's
# content and execs it, so the continue command (written into the flag by
# terminal.py at session-create time) rides the same path. Realized behavior
# for claude is identical — the flag content is `claude -c || claude`, eval'd in
# the same initial pane — which the golden/behavioral tests pin.
_ZELLIJ_WRAPPER_SCRIPT = (
    '#!/bin/bash\n'
    'REAL_SHELL="${ZELLIJ_REAL_SHELL:-/bin/bash}"\n'
    'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"\n'
    'if [ -f "$INIT_FILE" ]; then\n'
    '    CMD="$(cat "$INIT_FILE")"\n'
    '    rm -f "$INIT_FILE"\n'
    '    eval "$CMD"\n'
    '    exit 0\n'
    'fi\n'
    'exec "$REAL_SHELL" "$@"\n'
)


def build_zellij_wrapper_script():
    """Return the generalized zellij init wrapper script.

    The script reads the per-session flag file, removes it, and execs its
    content. ``terminal.py`` writes the continue command (from
    ``build_zellij_continue_command``) into that flag. Kept as a function (not
    a bare constant) so callers import a stable seam.
    """
    return _ZELLIJ_WRAPPER_SCRIPT


# ---------------------------------------------------------------------------
# ClaudeAdapter — the sole harness, wraps today's behavior exactly.
# ---------------------------------------------------------------------------

class ClaudeAdapter:
    """The Claude Code adapter: full capabilities, the default (and only) harness.

    Spawn modes:
      * ``fresh``    — ``claude``
      * ``continue`` — ``claude -c`` with the trap/fallback wrapper
      * ``resume``   — ``claude --resume <session_id>``

    The binary is ``settings.resolved_claude_binary`` (honoring the
    ``claude_binary`` legacy key and its ``agents['claude']['binary']``
    migration). The env half delegates to ``models.build_spawn_env``, so custom
    providers route through direct env injection (the ollama-style
    ANTHROPIC_BASE_URL + per-tier model vars) exactly as ``claude-ollama`` does.
    """
    id = 'claude'
    display_name = 'Claude Code'
    # M-UX.10 (C7): shown in the one-shot toast when a spawn fails because the
    # binary isn't installed — a recovery path, not a raw bash error.
    install_hint = 'Install from claude.ai/code'
    caps = AgentCaps(
        continue_=True,
        resume_by_id=True,
        sessions=True,
        rich_status=True,
        model_select=True,
        headless_json=True,
    )

    def __init__(self):
        # HistoryReader lives in model.py (it imports no GTK at the class
        # level, but model.py does import gi at module scope, so we defer the
        # import to list_sessions to keep agents.py headless-importable).
        self._history = None

    # --- spawn contract ---------------------------------------------------

    def _binary(self, settings):
        return settings.resolved_claude_binary

    def fresh_argv(self, settings):
        return [self._binary(settings)]

    def continue_argv(self, settings):
        return [self._binary(settings), '-c']

    def resume_argv(self, settings, session_id):
        return [self._binary(settings), '--resume', session_id]

    def spawn_plan(self, settings, project, mode, session_id=None):
        """Build the ``SpawnPlan`` for a spawn request.

        ``mode`` is ``fresh`` | ``continue`` | ``resume``. The env half is
        delegated to ``models.build_spawn_env`` (custom-provider injection or
        native); the adapter is the only thing the spawn path consults for env.
        """
        if mode == 'resume':
            if not session_id:
                raise ValueError("resume mode requires a session_id")
            argv = self.resume_argv(settings, session_id)
        elif mode == 'fresh':
            argv = self.fresh_argv(settings)
        elif mode == 'continue':
            argv = build_continue_wrapper(
                self.continue_argv(settings), self.fresh_argv(settings),
                fallback=self.caps.continue_falls_back_to_fresh,
            )
        else:
            raise ValueError(f"unknown spawn mode: {mode!r}")

        from models import build_spawn_env
        env, reason = build_spawn_env(settings, project.path)
        return SpawnPlan(argv=argv, env=env, fallback_reason=reason)

    # --- zellij path ------------------------------------------------------

    def zellij_continue_command(self, settings, project=None):
        """The continue command string written into the per-session flag file.

        ``project`` is accepted for protocol uniformity; claude routes the
        model through env (not the command), so the command itself is
        project-independent. The fallback (``|| <fresh>``) is the adapter's
        declared policy, not the builder's hardcode.
        """
        return build_zellij_continue_command(
            self.continue_argv(settings), self.fresh_argv(settings),
            fallback=self.caps.continue_falls_back_to_fresh,
        )

    def zellij_spawn_env(self, settings, project):
        """Env override for a NEW zellij session's server, or ``None``.

        The custom-provider env can only be applied when the zellij server is
        first created (an attach inherits the server's existing env). This
        delegates to ``models.build_spawn_env``; ``terminal.py`` consults only
        this method, never ``models`` directly. Returns the full env dict when a
        custom provider is in play, or ``(None, None)`` for the native path. The
        second tuple element is the fallback_reason (so ``spawn_zellij`` can
        surface a provider-unavailable toast on the create path exactly as the
        direct path does).
        """
        from models import build_spawn_env
        return build_spawn_env(settings, project.path)

    # --- sessions contract ------------------------------------------------

    def list_sessions(self, project, settings=None):
        """Return the project's recent sessions as ``SessionRef``s.

        Delegates to the existing ``HistoryReader`` (``~/.claude/history.jsonl``,
        newest-first, capped at 7 as today). ``settings`` is accepted for
        protocol uniformity; claude ignores it. Imported lazily so this pure
        module stays headless-importable even though ``model.py`` pulls in gi.
        """
        from model import HistoryReader
        if self._history is None:
            self._history = HistoryReader()
        self._history.load()
        return [
            SessionRef(id=s.session_id, title=s.title, last_active=s.last_active)
            for s in self._history.get_sessions(project)
        ]


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------

ADAPTERS = {
    'claude': ClaudeAdapter(),
}

DEFAULT_AGENT = 'claude'


def get_adapter(agent_id, settings=None):
    """Return the adapter for ``agent_id``, falling back when it's unknown.

    Only ``'claude'`` is registered now; any other id (a stale session.json or
    settings override from the multi-harness era) resolves to claude so the
    spawn path never breaks. The signature (and the ``settings`` arg) is kept
    so callers don't change.
    """
    hit = ADAPTERS.get(agent_id)
    if hit is not None:
        return hit
    return ADAPTERS[DEFAULT_AGENT]


def fallback_adapter(settings=None):
    """The adapter to use when a requested harness isn't available.

    With a single-harness registry this is always Claude; kept for caller
    stability (window.py's unknown-harness warning path).
    """
    return ADAPTERS[DEFAULT_AGENT]


def resolve_adapter(agent_id, settings=None):
    """Resolve ``agent_id`` to ``(adapter, missing_name)``.

    Distinguishes default-resolution from named-but-missing so the UI can warn:

      * ``'claude'`` (or a falsy id — "use the default") → ``(adapter, None)``
      * a non-empty id that isn't ``'claude'`` →
        ``(adapter, <that id>)``; ``missing_name`` is the unknown id the caller
        asked for, so window.py can show a one-shot
        "harness 'X' not available — using Claude Code" toast.

    The returned adapter is always the claude one; only the diagnostic differs
    from ``get_adapter``. ``settings`` is optional for legacy single-arg callers.
    """
    if agent_id and agent_id in ADAPTERS:
        return ADAPTERS[agent_id], None
    if not agent_id:
        return fallback_adapter(settings), None
    return fallback_adapter(settings), agent_id