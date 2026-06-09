"""Agent adapter seam — ProjectMan's pluggable coding-agent backend.

ProjectMan owns four contracts (status, sessions, spawn, model); each agent
adapter translates its agent's native mechanisms onto them. This module is the
spawn + sessions seam: an ``AgentAdapter`` declares its capabilities and turns a
``(settings, project, mode)`` request into a concrete ``SpawnPlan`` (argv + env)
that ``terminal.py`` executes below the line, unchanged.

P1 ships exactly one adapter — ``ClaudeAdapter`` — wrapping today's behavior
bit-for-bit. It is the reference backend and the default. The module is pure
(no GTK, like ``zellij.py``/``session.py``) so it is fully unit-testable
headless; the golden tests in ``tests/test_agent_seam.py`` pin the Claude
adapter to the pre-refactor spawn argv/env/zellij strings.

Design source: docs/superpowers/specs/2026-06-09-llm-agnostic-agents-design.md
(Part I, "Architecture: four contracts" + "AgentAdapter protocol").
"""
import shlex
from dataclasses import dataclass


@dataclass
class AgentCaps:
    """What an adapter's agent supports. The UI degrades feature-by-feature
    on these flags rather than gating whole agents."""
    continue_: bool = False     # resume the most recent conversation
    resume_by_id: bool = False  # resume a specific session id
    sessions: bool = False      # per-project session enumeration (history expander)
    rich_status: bool = False   # lifecycle events → live status dots
    model_select: bool = False  # per-project model is meaningful
    headless_json: bool = False # `-p`-style structured output (PAA-relevant)


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
    path); a dict to override it (e.g. ccr injection). ``fallback_reason`` is a
    human-readable string when the adapter wanted a richer spawn but degraded
    (e.g. ccr unavailable → native), for the UI to surface; ``None`` otherwise.
    """
    argv: list
    env: dict | None = None
    fallback_reason: str | None = None


# ---------------------------------------------------------------------------
# Spawn-wrapper builders (pure string/argv construction; generalize the
# claude-specific wrappers currently hardcoded in terminal.py).
# ---------------------------------------------------------------------------

def build_continue_wrapper(continue_argv, fresh_argv):
    """Wrap a continue-then-fresh fallback in a bash trap/exit-code guard.

    Generalizes the wrapper currently hardcoded in ``terminal.py:spawn_claude``.
    It runs ``continue_argv`` (e.g. ``claude -c``); if that exits non-zero for a
    *non-signal* reason (claude -c exits 1 when there is no history to resume),
    it ``exec``s ``fresh_argv`` (e.g. ``claude``). Two guards keep PM's
    SIGTERM/SIGHUP from accidentally respawning a fresh agent:

      * the exit-code test rejects signal kills (status > 128);
      * the ``trap 'exit 143' TERM HUP`` rejects the graceful-but-nonzero case
        where the agent catches the signal and cleanly returns 1-128. Without
        the trap that case slips past the exit-code test and bash exec's a fresh
        agent that never saw the signal, leaving the project stuck "active".

    For claude's argvs (``continue_argv=[bin, '-c']``, ``fresh_argv=[bin]``) the
    returned list is byte-identical to the pre-refactor wrapper — pinned by the
    golden test. Returns a ``['bash', '-c', <script>]`` argv list.
    """
    cont = shlex.join(continue_argv)
    fresh = shlex.join(fresh_argv)
    return ['bash', '-c',
            f"trap 'exit 143' TERM HUP; {cont}; s=$?; "
            f'[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec {fresh}']


def build_zellij_continue_command(continue_argv, fresh_argv):
    """Shell command line the zellij init pane runs to continue-or-start.

    Generalizes the hardcoded ``claude -c || claude``. The per-session flag file
    now CONTAINS this string and the wrapper execs it, so any agent's continue
    command rides the same path. For claude's argvs the result is
    ``'claude -c || claude'`` — byte-identical to today (golden-pinned).
    """
    return f'{shlex.join(continue_argv)} || {shlex.join(fresh_argv)}'


# Generalized zellij shell wrapper. Unlike the pre-refactor script it does NOT
# hardcode `claude -c || claude`; it reads the per-session flag file's content
# and execs it, so each agent supplies its own continue command (written into
# the flag by terminal.py at session-create time). Realized behavior for claude
# is identical — the flag content is `claude -c || claude`, eval'd in the same
# initial pane — which the golden/behavioral tests pin.
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
    """Return the generalized zellij init wrapper script (agent-independent).

    The script reads the per-session flag file, removes it, and execs its
    content. ``terminal.py`` writes the agent's continue command (from
    ``build_zellij_continue_command``) into that flag. Kept as a function (not a
    bare constant) so callers import a stable seam.
    """
    return _ZELLIJ_WRAPPER_SCRIPT


# ---------------------------------------------------------------------------
# ClaudeAdapter — backend #0, wraps today's behavior exactly.
# ---------------------------------------------------------------------------

class ClaudeAdapter:
    """The Claude Code adapter: full capabilities, the default backend.

    Spawn modes:
      * ``fresh``    — ``claude``
      * ``continue`` — ``claude -c`` with the trap/fallback wrapper
      * ``resume``   — ``claude --resume <session_id>``

    The binary is ``settings.resolved_claude_binary`` (honoring the
    ``claude_binary`` legacy key and its ``agents['claude']['binary']``
    migration). The env half delegates to ``ccr.spawn_env`` (P0), so custom
    models route through claude-code-router exactly as before; ccr stays
    Claude-adapter-internal.
    """
    id = 'claude'
    display_name = 'Claude Code'
    caps = AgentCaps(
        continue_=True,
        resume_by_id=True,
        sessions=True,
        rich_status=True,
        model_select=True,
        headless_json=True,
    )

    def __init__(self):
        # HistoryReader lives in model.py this phase (it imports no GTK at the
        # class level, but model.py does import gi at module scope, so we defer
        # the import to list_sessions to keep agents.py headless-importable).
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

    def spawn_plan(self, settings, project, mode, session_id=None, **ccr_kwargs):
        """Build the ``SpawnPlan`` for a spawn request.

        ``mode`` is ``fresh`` | ``continue`` | ``resume``. ``ccr_kwargs`` are
        forwarded to ``ccr.spawn_env`` (``probe``/``sleep_fn``/``start_wait``)
        so tests can inject a probe and stay instant — production callers pass
        none and get the real socket-probe path.
        """
        if mode == 'resume':
            if not session_id:
                raise ValueError("resume mode requires a session_id")
            argv = self.resume_argv(settings, session_id)
        elif mode == 'fresh':
            argv = self.fresh_argv(settings)
        elif mode == 'continue':
            argv = build_continue_wrapper(
                self.continue_argv(settings), self.fresh_argv(settings)
            )
        else:
            raise ValueError(f"unknown spawn mode: {mode!r}")

        import ccr as _ccr
        env, reason = _ccr.spawn_env(settings, project.path, **ccr_kwargs)
        return SpawnPlan(argv=argv, env=env, fallback_reason=reason)

    # --- zellij path ------------------------------------------------------

    def zellij_continue_command(self, settings):
        """The continue command string written into the per-session flag file."""
        return build_zellij_continue_command(
            self.continue_argv(settings), self.fresh_argv(settings)
        )

    # --- sessions contract ------------------------------------------------

    def list_sessions(self, project):
        """Return the project's recent sessions as ``SessionRef``s.

        Delegates to the existing ``HistoryReader`` (``~/.claude/history.jsonl``,
        newest-first, capped at 7 as today). Imported lazily so this pure module
        stays headless-importable even though ``model.py`` pulls in gi.
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


def get_adapter(agent_id):
    """Return the adapter for ``agent_id``, defaulting to claude.

    An unknown id resolves to the Claude adapter — the safe default keeps a
    stale session.json/settings override (e.g. pointing at an agent removed
    later) from breaking restore.
    """
    return ADAPTERS.get(agent_id) or ADAPTERS['claude']
