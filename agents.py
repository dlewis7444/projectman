"""Agent adapter seam — ProjectMan's pluggable coding-agent backend.

ProjectMan owns four contracts (status, sessions, spawn, model); each agent
adapter translates its agent's native mechanisms onto them. This module is the
spawn + sessions seam: an ``AgentAdapter`` declares its capabilities and turns a
``(settings, project, mode)`` request into a concrete ``SpawnPlan`` (argv + env)
that ``terminal.py`` executes below the line, unchanged.

P1 shipped exactly one adapter — ``ClaudeAdapter`` — wrapping today's behavior
bit-for-bit. P2 makes the seam load-bearing (every consumer goes THROUGH it, not
around it) and adds ``OpencodeAdapter`` as the second, first-class backend. The
module is pure (no GTK, like ``zellij.py``/``session.py``) so it is fully
unit-testable headless; the golden tests in ``tests/test_agent_seam.py`` pin the
Claude adapter to the pre-refactor spawn argv/env/zellij strings.

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
    # Continue-fallback policy (M-P3.3): when continue (`-c`) finds nothing to
    # resume, does the wrapper fall back to a fresh session? The decision is the
    # ADAPTER's, not the wrapper's hardcode — an agent whose continue exits
    # non-zero for reasons OTHER than "nothing to continue" (a bad flag, an auth
    # error) must be able to refuse the fresh fallback so a resume error doesn't
    # silently launch a fresh agent. claude/opencode exit 1 cleanly on
    # nothing-to-continue, so they keep the fallback (True). Grok's
    # nothing-to-continue behavior is a bench UNKNOWN and plugs in here once
    # probed.
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

def build_continue_wrapper(continue_argv, fresh_argv, *, fallback=True):
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

    ``fallback`` is the ADAPTER's continue-fallback policy (M-P3.3,
    ``AgentCaps.continue_falls_back_to_fresh``): when False the wrapper runs the
    continue command alone (still under the signal trap) and NEVER exec's fresh —
    for an agent whose non-zero exit doesn't reliably mean "nothing to continue".

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

    Generalizes the hardcoded ``claude -c || claude``. The per-session flag file
    now CONTAINS this string and the wrapper execs it, so any agent's continue
    command rides the same path. ``fallback`` is the adapter's continue-fallback
    policy (M-P3.3): when False the command is the continue command ALONE — no
    ``|| <fresh>`` tail — so a resume error doesn't silently launch a fresh
    agent. For claude's argvs with the default ``fallback=True`` the result is
    ``'claude -c || claude'`` — byte-identical to today (golden-pinned).
    """
    cont = shlex.join(continue_argv)
    if not fallback:
        return cont
    return f'{cont} || {shlex.join(fresh_argv)}'


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

    def __init__(self, *, ccr_kwargs=None):
        # HistoryReader lives in model.py this phase (it imports no GTK at the
        # class level, but model.py does import gi at module scope, so we defer
        # the import to list_sessions to keep agents.py headless-importable).
        self._history = None
        # ccr probe/sleep_fn/start_wait injection is a CLAUDE-INTERNAL detail,
        # not part of the spawn protocol (m1/A4): the uniform ``spawn_plan``
        # signature is ``(settings, project, mode, session_id=None)`` across all
        # adapters. Tests that need an instant ccr probe construct a
        # ClaudeAdapter with ``ccr_kwargs={'probe': ...}`` instead of passing it
        # through the protocol call.
        self._ccr_kwargs = dict(ccr_kwargs) if ccr_kwargs else {}

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

        ``mode`` is ``fresh`` | ``continue`` | ``resume``. The signature is
        uniform across adapters (m1/A4); ccr probe injection for tests is a
        Claude-internal ctor param (``ccr_kwargs``), not a protocol argument.
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

        import ccr as _ccr
        env, reason = _ccr.spawn_env(settings, project.path, **self._ccr_kwargs)
        return SpawnPlan(argv=argv, env=env, fallback_reason=reason)

    # --- zellij path ------------------------------------------------------

    def zellij_continue_command(self, settings, project=None):
        """The continue command string written into the per-session flag file.

        ``project`` is accepted for protocol uniformity with model-as-argv
        adapters (opencode folds the per-project model into this string); claude
        routes the model through ccr env, so the command itself is
        project-independent. The fallback (``|| <fresh>``) is the adapter's
        declared policy (M-P3.3), not the builder's hardcode.
        """
        return build_zellij_continue_command(
            self.continue_argv(settings), self.fresh_argv(settings),
            fallback=self.caps.continue_falls_back_to_fresh,
        )

    def zellij_spawn_env(self, settings, project):
        """Env override for a NEW zellij session's server, or ``None`` (A3/M3).

        The ccr custom-model env can only be applied when the zellij server is
        first created (an attach inherits the server's existing env). This moves
        the old ``terminal.py:_claude_env`` logic — and the hardcoded
        ``ANTHROPIC_*`` key list — behind the adapter: ``spawn_zellij`` consults
        only this method, never ccr directly. Returns the full ccr env dict
        (inherited environ + the four ANTHROPIC vars) when ccr is in play, or
        ``None`` for the native path. The second tuple element is the
        fallback_reason (so ``spawn_zellij`` can surface a ccr toast on the
        create path exactly as the direct path does).
        """
        import ccr as _ccr
        return _ccr.spawn_env(settings, project.path, **self._ccr_kwargs)

    # --- sessions contract ------------------------------------------------

    def list_sessions(self, project, settings=None):
        """Return the project's recent sessions as ``SessionRef``s.

        Delegates to the existing ``HistoryReader`` (``~/.claude/history.jsonl``,
        newest-first, capped at 7 as today). ``HistoryReader`` is Claude-internal
        plumbing now (A1): no consumer reads it directly — the sidebar expander
        goes through ``list_sessions``. ``settings`` is accepted for protocol
        uniformity (opencode uses it to resolve a custom binary); claude ignores
        it. Imported lazily so this pure module stays headless-importable even
        though ``model.py`` pulls in gi.
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
# OpencodeAdapter — backend #1 (P2 pilot). The seam's first non-claude consumer.
# ---------------------------------------------------------------------------

# Session-list parsing helpers (pure; fixture-tested in
# tests/test_opencode_sessions.py). They are module-level so tests can exercise
# them directly without standing up the adapter or the CLI.

_SESSIONS_CAP = 7  # parity with the claude expander


def parse_session_list_json(text, project_path, *, realpath=None, cap=_SESSIONS_CAP):
    """Parse ``opencode session list --format json`` into SessionRefs.

    The JSON form is the only route that can filter per-project: each entry
    carries a ``directory`` (the cwd the session ran in) which the table form
    omits. Entries whose ``directory`` resolves to ``project_path`` are kept,
    newest-first by ``updated`` (epoch ms, same unit as claude's last_active),
    capped at ``cap``.

    Defensive: tolerates a missing/short title, missing ``updated`` (falls back
    to ``created``/0), non-list top-level (returns []), and entries that aren't
    dicts. ``realpath`` is injectable (defaults to ``os.path.realpath``) so the
    directory comparison normalises symlinks exactly like HistoryReader.
    """
    import json as _json
    import os as _os
    if realpath is None:
        realpath = _os.path.realpath
    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    try:
        target = realpath(project_path)
    except OSError:
        target = project_path
    refs = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        directory = entry.get('directory') or entry.get('worktree') or ''
        if not directory:
            continue
        try:
            if realpath(directory) != target:
                continue
        except OSError:
            if directory != target:
                continue
        sid = entry.get('id') or entry.get('sessionID') or ''
        if not sid:
            continue
        last = entry.get('updated')
        if last is None:
            last = entry.get('created', 0)
        refs.append(SessionRef(
            id=str(sid),
            title=str(entry.get('title') or ''),
            last_active=int(last) if isinstance(last, (int, float)) else 0,
        ))
    refs.sort(key=lambda r: r.last_active, reverse=True)
    return refs[:cap]


def parse_session_list_table(text, *, cap=_SESSIONS_CAP):
    """Parse the default ``opencode session list`` TABLE output.

    Returns ``(id, title)`` tuples in display order. NOTE: the table has no
    directory column, so this CANNOT filter per-project — it exists for
    completeness/diagnostics, not for the expander. Skips the header line and
    the box-drawing separator; tolerates ragged whitespace.
    """
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # Skip the box-drawing separator row (── …).
        if set(line.strip()) <= {'─', '━', '-', ' '}:
            continue
        # Skip the header row.
        if line.lstrip().startswith('Session ID'):
            continue
        # A session row begins with the ses_ id token.
        parts = line.split(None, 1)
        if not parts or not parts[0].startswith('ses_'):
            continue
        sid = parts[0]
        rest = parts[1] if len(parts) > 1 else ''
        # The trailing "Updated" column is whitespace-separated from the title;
        # we keep the whole remainder as the title (best-effort — the table is a
        # fallback diagnostic, not the per-project source).
        out.append((sid, rest.strip()))
        if len(out) >= cap:
            break
    return out


def scan_storage_sessions(storage_dir, project_path, *, realpath=None,
                          cap=_SESSIONS_CAP):
    """Storage-scan fallback over ``~/.local/share/opencode/storage``.

    Maps ``project_path`` to a projectId via ``storage/project/*.json``'s
    ``worktree`` field, then reads ``storage/session/<projectId>/*.json``. The
    session record's timestamp is read from ``updated`` or ``time.updated``
    (layout has drifted across versions — tolerate both). Newest-first, capped.

    Returns [] on any structural surprise — this is the last-resort path and
    must never raise. ``realpath`` injectable as above.

    KNOWN GAP (P2 VM gate, both 1.16.2 and 1.17.0): current opencode builds
    replaced this file tree with a SQLite store (``opencode.db``), which this
    scan does NOT read — on those versions it correctly returns [] and the
    cwd-scoped CLI is the only working path. The file-layout scan is kept for
    genuinely old builds that still have the tree; SQLite support is P3
    hardening, deliberately not attempted here.
    """
    import json as _json
    import os as _os
    if realpath is None:
        realpath = _os.path.realpath
    try:
        target = realpath(project_path)
    except OSError:
        target = project_path

    project_id = None
    proj_dir = _os.path.join(storage_dir, 'project')
    try:
        proj_entries = list(_os.scandir(proj_dir))
    except (FileNotFoundError, NotADirectoryError, OSError):
        proj_entries = []
    for entry in proj_entries:
        if not entry.name.endswith('.json'):
            continue
        try:
            with open(entry.path) as f:
                meta = _json.load(f)
        except (OSError, ValueError):
            continue
        worktree = meta.get('worktree') or meta.get('directory') or ''
        if not worktree:
            continue
        try:
            same = realpath(worktree) == target
        except OSError:
            same = worktree == target
        if same:
            project_id = meta.get('id') or entry.name[:-len('.json')]
            break
    if not project_id:
        return []

    sess_dir = _os.path.join(storage_dir, 'session', project_id)
    refs = []
    try:
        sess_entries = list(_os.scandir(sess_dir))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    for entry in sess_entries:
        if not entry.name.endswith('.json'):
            continue
        try:
            with open(entry.path) as f:
                rec = _json.load(f)
        except (OSError, ValueError):
            continue
        sid = rec.get('id') or entry.name[:-len('.json')]
        last = rec.get('updated')
        if last is None:
            t = rec.get('time')
            if isinstance(t, dict):
                last = t.get('updated', t.get('created', 0))
            else:
                last = rec.get('created', 0)
        refs.append(SessionRef(
            id=str(sid),
            title=str(rec.get('title') or ''),
            last_active=int(last) if isinstance(last, (int, float)) else 0,
        ))
    refs.sort(key=lambda r: r.last_active, reverse=True)
    return refs[:cap]


class OpencodeAdapter:
    """opencode adapter — the P2 pilot, full capabilities, natively
    multi-provider.

    Spawn modes:
      * ``fresh``    — ``opencode``
      * ``continue`` — ``opencode -c`` with the trap/fallback wrapper
      * ``resume``   — ``opencode -s <id>``

    Model: per-project model string is passed verbatim as ``-m <value>`` when
    set (e.g. ``ollama/qwen3.5:cloud`` — opencode's native provider/model form,
    aligned with the VM's opencode.json provider). NO env injection, NO ccr —
    opencode reaches providers itself. Empty/default → no ``-m`` (opencode uses
    its own configured default).

    Sessions: ``opencode session list --format json`` filtered by directory
    (primary), storage-scan fallback. See the parser helpers above.
    """
    id = 'opencode'
    display_name = 'opencode'
    # M-UX.10 (C7): recovery hint surfaced on a missing-binary spawn failure.
    install_hint = 'Install from opencode.ai'
    caps = AgentCaps(
        continue_=True,
        resume_by_id=True,
        sessions=True,
        rich_status=True,
        model_select=True,
        headless_json=True,
    )

    def __init__(self, *, run_fn=None, storage_dir=None):
        # ``run_fn`` runs the session-list CLI; injectable so tests never shell
        # out. It takes (argv:list, cwd:str|None) and returns (rc:int,
        # stdout:str) or None on failure — cwd is part of the contract because
        # `opencode session list` is cwd-scoped (VM gate finding).
        # ``storage_dir`` overrides the storage-scan root for tests.
        self._run_fn = run_fn
        self._storage_dir = storage_dir

    # --- binary -----------------------------------------------------------

    def _binary(self, settings):
        """``agents['opencode']['binary']`` if set, else ``opencode``."""
        cfg = settings.agents.get('opencode') if isinstance(settings.agents, dict) else None
        if isinstance(cfg, dict):
            b = (cfg.get('binary') or '').strip()
            if b:
                return b
        return 'opencode'

    def _model_args(self, settings, project):
        """``['-m', value]`` for a set per-project model, else ``[]``."""
        model = settings.effective_model(project.path)
        if model and self.caps.model_select:
            return ['-m', model]
        return []

    # --- spawn contract ---------------------------------------------------

    def fresh_argv(self, settings, project):
        return [self._binary(settings)] + self._model_args(settings, project)

    def continue_argv(self, settings, project):
        # ``-m <model>`` before ``-c`` per the spec's example form
        # (``opencode -m <model> -c``); flag order is otherwise immaterial.
        return ([self._binary(settings)] + self._model_args(settings, project)
                + ['-c'])

    def resume_argv(self, settings, project, session_id):
        return ([self._binary(settings)] + self._model_args(settings, project)
                + ['-s', session_id])

    def spawn_plan(self, settings, project, mode, session_id=None):
        """Uniform spawn contract (A4). opencode needs no env override — it is
        natively multi-provider — so env is always None and there is no ccr
        fallback to surface.

        For ``continue`` the model flag must fold INTO the fallback wrapper so
        BOTH the ``opencode -c`` attempt and the bare-``opencode`` fallback
        carry ``-m <model>`` (review n3 / A3): the flag-file content is
        ``opencode -m <model> -c || opencode -m <model>``.
        """
        if mode == 'resume':
            if not session_id:
                raise ValueError("resume mode requires a session_id")
            argv = self.resume_argv(settings, project, session_id)
        elif mode == 'fresh':
            argv = self.fresh_argv(settings, project)
        elif mode == 'continue':
            argv = build_continue_wrapper(
                self.continue_argv(settings, project),
                self.fresh_argv(settings, project),
                fallback=self.caps.continue_falls_back_to_fresh,
            )
        else:
            raise ValueError(f"unknown spawn mode: {mode!r}")
        return SpawnPlan(argv=argv, env=None, fallback_reason=None)

    # --- zellij path ------------------------------------------------------

    def zellij_continue_command(self, settings, project=None):
        """Flag-file content. The model folds into BOTH halves so the create
        pane's continue-or-fresh both target the chosen provider/model:
        ``opencode -m <model> -c || opencode -m <model>``. The fallback is the
        adapter's declared policy (M-P3.3), not the builder's hardcode.
        """
        fallback = self.caps.continue_falls_back_to_fresh
        if project is None:
            # Defensive: a model-less command if no project context (shouldn't
            # happen via terminal.py, which always passes the project).
            return build_zellij_continue_command(
                [self._binary(settings), '-c'], [self._binary(settings)],
                fallback=fallback)
        return build_zellij_continue_command(
            self.continue_argv(settings, project),
            self.fresh_argv(settings, project),
            fallback=fallback,
        )

    def zellij_spawn_env(self, settings, project):
        """No env override for opencode (A3) — natively multi-provider, no ccr.
        Returns ``(None, None)`` so terminal.py inherits os.environ unchanged.
        """
        return (None, None)

    # --- sessions contract ------------------------------------------------

    def _run(self, argv, cwd=None):
        """Run a session-list command in ``cwd`` → (rc, stdout) or None.

        ``cwd`` is part of the run contract, not an afterthought: the P2 VM
        gate found ``opencode session list`` is CWD-SCOPED — invoked from the
        wrong directory it reports another project's (or zero) sessions even
        though the JSON carries per-entry directories. Injectable ``run_fn``
        takes ``(argv, cwd)`` for the same reason. A bad/missing cwd makes
        subprocess.run raise (caught → None → storage fallback). Never raises.
        """
        if self._run_fn is not None:
            return self._run_fn(argv, cwd)
        import subprocess
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL, cwd=cwd)
            return (r.returncode, r.stdout)
        except (OSError, subprocess.SubprocessError):
            return None

    def list_sessions(self, project, settings=None):
        """Recent sessions for ``project`` as SessionRefs (cap 7, newest-first).

        Primary: ``opencode session list --format json -n N`` run **from the
        project directory** (the CLI is cwd-scoped — VM gate finding; running
        it from PM's own cwd returned 0 rows while the same command from the
        project dir returned the real sessions) and then filtered by each
        entry's ``directory``. Fallback: storage scan over the OLD file layout
        (``storage/project`` + ``storage/session``) — KNOWN GAP: current
        opencode builds (1.16+/1.17) store sessions in SQLite (``opencode.db``),
        which this fallback does not read, so on those versions the CLI is the
        only working path; SQLite support is P3 hardening. Defensive
        throughout — any failure in one layer falls through to the next; a
        total failure returns []. ``settings`` is optional (used only to
        resolve a custom binary); the sidebar passes it via the adapter call.
        """
        binary = self._binary(settings) if settings is not None else 'opencode'
        # Ask for a few extra rows before the per-directory filter trims to cap.
        result = self._run([binary, 'session', 'list', '--format', 'json',
                            '-n', '200'], cwd=project.path)
        if result is not None:
            rc, out = result
            if rc == 0 and out and out.strip():
                refs = parse_session_list_json(out, project.path)
                # Even rc==0 with valid-but-empty JSON is a legitimate "no
                # sessions" answer; only fall through when JSON parsing yielded
                # nothing AND the output wasn't valid JSON list. parse returns []
                # for both, so prefer the storage scan only when the CLI failed.
                if refs:
                    return refs
        # Fallback: storage scan (old file layout only — see docstring).
        import os as _os
        storage = self._storage_dir or _os.path.expanduser(
            '~/.local/share/opencode/storage')
        return scan_storage_sessions(storage, project.path)


# ---------------------------------------------------------------------------
# GrokAdapter — backend #2 (P3). xAI "Grok Build" (binary ``grok``).
# ---------------------------------------------------------------------------

# Session-list parser for ``grok sessions list`` (pure; fixture-tested in
# tests/test_grok_sessions.py). Module-level so tests exercise it directly.

def parse_grok_session_list(text, *, cap=_SESSIONS_CAP):
    """Parse ``grok sessions list`` fixed-column text into SessionRefs.

    The probe (scripts-local/evidence/p3-grok-probe/probe.md Q4, raw output)
    found grok ships a real, auth-free, cwd-scoped ``grok sessions list``
    subcommand — but NO ``--json`` flag (the F2 ruling: CLI-first, parse the
    columns). The observed shape is a header row then one row per session::

        SESSION ID                            CREATED     UPDATED     STATUS      SUMMARY
        019eb297-fa74-7741-863e-d8aa822ac7bf  2026-06-10  2026-06-10  local  Reply with exactly: hook-test-ok

    Columns are whitespace-separated and the SUMMARY is free text that may
    itself contain spaces, so we split off the four fixed leading tokens
    (id, created, updated, status) and keep the remainder as the title. The
    SESSION ID is the UUIDv7 (``SessionRef.id`` = it, verbatim — resume uses it).

    The CLI already emits rows newest-first by UPDATED (probe-observed: the
    later-updated session leads). UPDATED is DATE-ONLY granularity, so within a
    single day the date can't break ties — but a UUIDv7 id is millisecond
    time-ordered, so we preserve the CLI's order and DO NOT re-sort by the
    coarse date (a date-key sort would scramble same-day rows). ``last_active``
    is the UPDATED date parsed to an epoch (00:00 UTC) for the expander's
    display only; ordering rides the CLI's newest-first emission, capped at
    ``cap`` (parity with the other expanders).

    Defensive: the header is skipped (its first token is ``SESSION``, never a
    UUID); blank lines and any row whose first token isn't a UUID-shaped
    36-char hyphenated id are ignored; a row missing the trailing columns
    yields an empty title rather than raising. Never raises.
    """
    import re as _re
    import calendar as _calendar
    import time as _time
    _uuid_re = _re.compile(
        r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
        r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

    def _date_to_epoch(s):
        try:
            return _calendar.timegm(_time.strptime(s, '%Y-%m-%d'))
        except (ValueError, TypeError):
            return 0

    refs = []
    for raw in (text or '').splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Split into the four fixed leading tokens + free-text remainder.
        parts = line.split(None, 4)
        if not parts:
            continue
        sid = parts[0]
        if not _uuid_re.match(sid):
            # Header row ("SESSION ID …") and any non-session noise.
            continue
        updated = parts[2] if len(parts) > 2 else ''
        title = parts[4].strip() if len(parts) > 4 else ''
        refs.append(SessionRef(
            id=sid,
            title=title,
            last_active=_date_to_epoch(updated),
        ))
        if len(refs) >= cap:
            break
    return refs


class GrokAdapter:
    """xAI "Grok Build" adapter — backend #2 (P3), full capabilities.

    Spawn modes:
      * ``fresh``    — ``grok``
      * ``continue`` — ``grok -c`` with the trap/fallback wrapper. The probe
        (probe.md Q1) observed ``grok -c`` exits 1 with "No session found for
        current directory" when there's nothing to continue — clean, claude-like
        — so ``caps.continue_falls_back_to_fresh=True`` and the standard
        ``-c || fresh`` wrapper applies unchanged (F1).
      * ``resume``   — ``grok -r <id>`` (F2: ids are UUIDv7 from
        ``grok sessions list``; local-id resume is auth-free per the probe).

    Model: per-project model string passed verbatim as ``-m <value>`` when set
    + ``caps.model_select``. Grok reaches custom OpenAI-compatible endpoints
    (the ollama pool) via its OWN ``~/.grok/config.toml`` ``[model.<key>]`` —
    so the model id here is a grok config KEY (e.g. ``pool-qwen``), and PM
    injects NO env and NO ccr. Empty/default → no ``-m``.

    NO ``--no-auto-update`` injection (F5: user's tool, user's policy — the
    bench pins versions bench-side via ``[cli] auto_update = false``). NO
    ``--no-alt-screen`` injection (F7: alt-screen is fine in VTE, claude/opencode
    precedent; reserved as a documented fallback only).

    LEADER DAEMON (F6): grok runs a persistent client/leader architecture
    (``~/.grok/leader.sock``). PM's pane child is a CLIENT; the leader is
    grok-owned and survives a client SIGTERM (analogous to the zellij server).
    PM does NO lifecycle management of the leader — group-kill
    (emergency_shutdown / _kill_child) takes the client only, by design.
    """
    id = 'grok'
    display_name = 'Grok Build'
    # M-UX.10 (C7): recovery hint surfaced on a missing-binary spawn failure —
    # the curl one-liner from the README's "Installing Grok Build".
    install_hint = 'Install: curl -fsSL https://x.ai/cli/install.sh | bash'
    caps = AgentCaps(
        continue_=True,
        resume_by_id=True,
        sessions=True,
        rich_status=True,
        model_select=True,
        headless_json=True,
        # F1: `grok -c` exits 1 cleanly on nothing-to-continue (probe Q1), so
        # the standard `-c || fresh` fallback applies — same as claude/opencode.
        continue_falls_back_to_fresh=True,
    )

    def __init__(self, *, run_fn=None):
        # ``run_fn(argv, cwd) -> (rc, stdout) | None`` runs the session-list CLI;
        # injectable so tests never shell out. cwd is part of the contract:
        # ``grok sessions list`` is cwd-scoped (probe Q4 — run it from the target
        # project dir), exactly like opencode's.
        self._run_fn = run_fn

    # --- binary -----------------------------------------------------------

    def _binary(self, settings):
        """``agents['grok']['binary']`` if set, else ``grok``."""
        cfg = settings.agents.get('grok') if isinstance(settings.agents, dict) else None
        if isinstance(cfg, dict):
            b = (cfg.get('binary') or '').strip()
            if b:
                return b
        return 'grok'

    def _model_args(self, settings, project):
        """``['-m', value]`` for a set per-project model, else ``[]``."""
        model = settings.effective_model(project.path)
        if model and self.caps.model_select:
            return ['-m', model]
        return []

    # --- spawn contract ---------------------------------------------------

    def fresh_argv(self, settings, project):
        return [self._binary(settings)] + self._model_args(settings, project)

    def continue_argv(self, settings, project):
        return ([self._binary(settings)] + self._model_args(settings, project)
                + ['-c'])

    def resume_argv(self, settings, project, session_id):
        return ([self._binary(settings)] + self._model_args(settings, project)
                + ['-r', session_id])

    def spawn_plan(self, settings, project, mode, session_id=None):
        """Uniform spawn contract (A4). grok reaches providers via its own
        config.toml, so env is always None and there is no ccr fallback.

        For ``continue`` the model flag folds INTO the fallback wrapper so BOTH
        the ``grok -c`` attempt and the bare-``grok`` fallback carry
        ``-m <model>`` (mirrors opencode's review-n3 fix): the flag-file content
        is ``grok -m <model> -c || grok -m <model>``.
        """
        if mode == 'resume':
            if not session_id:
                raise ValueError("resume mode requires a session_id")
            argv = self.resume_argv(settings, project, session_id)
        elif mode == 'fresh':
            argv = self.fresh_argv(settings, project)
        elif mode == 'continue':
            argv = build_continue_wrapper(
                self.continue_argv(settings, project),
                self.fresh_argv(settings, project),
                fallback=self.caps.continue_falls_back_to_fresh,
            )
        else:
            raise ValueError(f"unknown spawn mode: {mode!r}")
        return SpawnPlan(argv=argv, env=None, fallback_reason=None)

    # --- zellij path ------------------------------------------------------

    def zellij_continue_command(self, settings, project=None):
        """Flag-file content. The model folds into BOTH halves (like opencode)
        so the create pane's continue-or-fresh both target the chosen grok
        model key: ``grok -m <model> -c || grok -m <model>``. The fallback is
        the adapter's declared policy (M-P3.3), not the builder's hardcode.
        """
        fallback = self.caps.continue_falls_back_to_fresh
        if project is None:
            return build_zellij_continue_command(
                [self._binary(settings), '-c'], [self._binary(settings)],
                fallback=fallback)
        return build_zellij_continue_command(
            self.continue_argv(settings, project),
            self.fresh_argv(settings, project),
            fallback=fallback,
        )

    def zellij_spawn_env(self, settings, project):
        """No env override for grok (F5/B5) — grok reaches the pool through its
        own ``~/.grok/config.toml``, no ccr, no PM env injection. Returns
        ``(None, None)`` so terminal.py inherits os.environ unchanged.
        """
        return (None, None)

    # --- sessions contract ------------------------------------------------

    def _run(self, argv, cwd=None):
        """Run ``grok sessions list`` in ``cwd`` → (rc, stdout) or None.

        ``cwd`` is part of the run contract: ``grok sessions list`` is
        CWD-SCOPED (probe Q4 — it lists the invoking directory's sessions), the
        same constraint opencode's lister carries. Injectable ``run_fn`` takes
        ``(argv, cwd)``. Never raises (any subprocess error → None → []).
        """
        if self._run_fn is not None:
            return self._run_fn(argv, cwd)
        import subprocess
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL, cwd=cwd)
            return (r.returncode, r.stdout)
        except (OSError, subprocess.SubprocessError):
            return None

    def list_sessions(self, project, settings=None):
        """Recent grok sessions for ``project`` as SessionRefs (cap 7).

        CLI-first (F2): ``grok sessions list -n N`` run **from the project
        directory** (cwd-scoped), parsed by ``parse_grok_session_list``. The CLI
        emits newest-first by UPDATED and is authoritative (no storage parsing —
        the JSON/JSONL session dirs exist but the CLI wins, P2 doctrine).
        Defensive: a missing/failed CLI returns []. ``settings`` resolves a
        custom binary; the sidebar passes it via the adapter call.
        """
        binary = self._binary(settings) if settings is not None else 'grok'
        # Ask for a few extra rows; the parser caps to _SESSIONS_CAP.
        result = self._run([binary, 'sessions', 'list', '-n', '50'],
                           cwd=project.path)
        if result is not None:
            rc, out = result
            if rc == 0 and out and out.strip():
                return parse_grok_session_list(out)
        return []


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------

ADAPTERS = {
    'claude': ClaudeAdapter(),
    'opencode': OpencodeAdapter(),
    'grok': GrokAdapter(),
}

DEFAULT_AGENT = 'claude'

# Ids that ship with ProjectMan. A custom/user-supplied adapter may NEVER claim
# one of these (M-P3.5): builtins win, no silent dict shadowing.
BUILTIN_AGENT_IDS = frozenset(ADAPTERS)


def register_adapter(adapter):
    """Register a custom adapter, REFUSING any id collision (M-P3.5).

    The registry was a plain dict, so a custom adapter whose ``id`` equalled a
    builtin (``claude``/``opencode``) silently overwrote it — a custom 'claude'
    shadowed ``ClaudeAdapter`` with no warning. This is the only guarded entry
    point for adding adapters at runtime (settings-loaded customs go through it):
    it raises ``ValueError`` loudly on a collision rather than clobbering, so a
    builtin can never be replaced and two customs can't fight over one id.

    Precedence is explicit: builtins always win; a duplicate of ANY already
    registered id (builtin or a prior custom) is refused. Returns the registered
    adapter on success. (Builtins are wired into ``ADAPTERS`` at import time, not
    through this function.)
    """
    agent_id = getattr(adapter, 'id', None)
    if not agent_id:
        raise ValueError("adapter must declare a non-empty id")
    if agent_id in BUILTIN_AGENT_IDS:
        raise ValueError(
            f"adapter id {agent_id!r} collides with a built-in agent; "
            "built-in adapters cannot be replaced"
        )
    if agent_id in ADAPTERS:
        raise ValueError(
            f"adapter id {agent_id!r} is already registered; "
            "ids must be unique"
        )
    ADAPTERS[agent_id] = adapter
    return adapter


def get_adapter(agent_id, settings=None):
    """Return the adapter for ``agent_id``, falling back when it's unknown.

    A registered id returns its adapter. An unknown id resolves to a FALLBACK so
    a stale session.json/settings override (e.g. pointing at an agent removed
    later) never breaks the spawn path. The fallback honors the M-P3.2 contract:

      * ``settings`` given → ``fallback_adapter(settings)`` (``agent_default``
        first, then first-available) — so the Claude-less promise holds even
        with a stale/bogus override: a fleet defaulting to opencode falls back
        to opencode, never silently to claude;
      * ``settings`` omitted → the legacy claude default, so single-arg callers
        keep today's exact behavior (the spawn path never breaks on a bad id).

    Callers that need to TELL the difference between "claude was asked for" and
    "X was asked for but is missing" use ``resolve_adapter`` instead (A6/m3);
    this function deliberately hides the miss.
    """
    hit = ADAPTERS.get(agent_id)
    if hit is not None:
        return hit
    if settings is not None:
        return fallback_adapter(settings)
    return ADAPTERS[DEFAULT_AGENT]


# ---------------------------------------------------------------------------
# Settings → Agents page helpers (pure; tested in test_agents_settings_page.py).
# The bridge-install + doctor logic lives here so it is headless-testable and
# reused by both install.sh's intent and the GUI button.
# ---------------------------------------------------------------------------

def _grok_hook_json_transform(text, home):
    """F12b: rewrite the hook commands to the absolute installed script path.

    The repo copy of ``bridges/grok/projectman.json`` keeps the portable
    ``python3 ~/.grok/hooks/projectman-status.py`` form; the INSTALLED copy
    must not rely on grok shell-expanding ``~``, so install time rewrites every
    command to ``python3 /abs/home/.grok/hooks/projectman-status.py``. JSON-
    aware (parse → rewrite command fields → re-dump) so an exotic ``home`` path
    can never break the JSON encoding; unparseable content is returned
    untouched (copied verbatim — the selftest/bench would catch it).
    """
    import json as _json
    import os as _os
    script_abs = _os.path.join(home, '.grok', 'hooks', 'projectman-status.py')
    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        return text
    hooks = data.get('hooks') if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for matchers in hooks.values():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                cmds = matcher.get('hooks') if isinstance(matcher, dict) else None
                if not isinstance(cmds, list):
                    continue
                for cmd in cmds:
                    if isinstance(cmd, dict) and isinstance(cmd.get('command'), str):
                        cmd['command'] = cmd['command'].replace(
                            '~/.grok/hooks/projectman-status.py', script_abs)
    return _json.dumps(data, indent=2) + '\n'


# Per-agent bridge manifest (F12a): the ONE definition of what files constitute
# each agent's status bridge, shared by the GUI "Install bridge" button AND
# install.sh (which delegates here via ``python3 -c``). Each entry:
#   src        — source file within the app tree, (subdir, filename) under
#                ``bridges/``
#   dest       — destination path relative to $HOME
#   executable — ensure the exec bit on the installed file (grok runs the
#                status script directly)
#   transform  — optional ``f(text, home) -> text`` applied to the content at
#                install time (F12b: the grok JSON's absolute-path rewrite);
#                idempotency compares the TRANSFORMED content to the dest.
_BRIDGE_MANIFEST = {
    'opencode': [
        {'src': ('opencode', 'projectman.js'),
         'dest': '.config/opencode/plugins/projectman.js',
         'executable': False, 'transform': None},
    ],
    'grok': [
        {'src': ('grok', 'projectman.json'),
         'dest': '.grok/hooks/projectman.json',
         'executable': False, 'transform': _grok_hook_json_transform},
        {'src': ('grok', 'projectman-status.py'),
         'dest': '.grok/hooks/projectman-status.py',
         'executable': True, 'transform': None},
    ],
}


def agent_bridge_source(app_dir, agent_id):
    """Absolute path to ``agent_id``'s PRIMARY status-bridge source, or None.

    ``app_dir`` is the installed/app directory (``bridges/`` lives under it).
    The primary source is the manifest's first entry (the hook/plugin
    definition); the Settings page uses this for "does this agent have an
    installable bridge". Returns None when the agent has no bridge or the
    primary file is absent.
    """
    import os as _os
    manifest = _BRIDGE_MANIFEST.get(agent_id)
    if not manifest:
        return None
    path = _os.path.join(app_dir, 'bridges', *manifest[0]['src'])
    return path if _os.path.exists(path) else None


def install_agent_bridge(app_dir, agent_id, *, home=None):
    """Install ``agent_id``'s status bridge — ALL its files — idempotently.

    F12a: the install is manifest-driven and multi-file (grok = hook JSON +
    executable status script; opencode = one plugin file), and this one
    function is the shared machinery behind BOTH the GUI button and
    install.sh's bridge steps — no second copy of the file list exists.
    Per-file: content is transformed when the manifest says so (F12b — the
    grok JSON gets its commands rewritten to the absolute installed script
    path), compared against the existing dest (transformed-source vs dest, so
    the rewrite stays idempotent), copied when missing/different, and given
    the exec bit when required.

    Returns ``'installed'`` (something was written/repaired) | ``'already'``
    (every file present, current, and correctly executable) | ``'no-bridge'``
    | ``'missing-source'`` (ANY manifest source absent → nothing installed) |
    ``'error'``. ``home`` overrides ``~`` for tests.
    """
    import os as _os
    if home is None:
        home = _os.path.expanduser('~')
    manifest = _BRIDGE_MANIFEST.get(agent_id)
    if not manifest:
        return 'no-bridge'
    # Resolve and read every source first: a partial bridge must never land.
    plans = []
    for spec in manifest:
        src = _os.path.join(app_dir, 'bridges', *spec['src'])
        if not _os.path.exists(src):
            return 'missing-source'
        plans.append((src, spec))
    changed = False
    try:
        for src, spec in plans:
            with open(src, 'rb') as f:
                content = f.read()
            transform = spec.get('transform')
            if transform is not None:
                content = transform(content.decode('utf-8'), home).encode('utf-8')
            dest = _os.path.join(home, spec['dest'])
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            same = False
            if _os.path.exists(dest):
                with open(dest, 'rb') as f:
                    same = f.read() == content
            if not same:
                with open(dest, 'wb') as f:
                    f.write(content)
                changed = True
            if spec.get('executable'):
                mode = _os.stat(dest).st_mode
                if not (mode & 0o111):
                    _os.chmod(dest, mode | 0o111)
                    changed = True  # repairing a lost exec bit is a change
        return 'installed' if changed else 'already'
    except OSError:
        return 'error'


def bridge_state(app_dir, agent_id, *, home=None):
    """Report an agent's installed-bridge state WITHOUT installing (M-UX.8/C5).

    The Settings → Agents bridge button claimed "Install bridge" even when the
    bridge was already installed and current (sweep F8 — C5 SHOWN ≠ ACTUAL).
    This is the read-only twin of ``install_agent_bridge``: it runs the SAME
    manifest comparison (transformed source vs dest, exec-bit check) but copies
    nothing, so the button can reflect machine state. Returns one of:

      * ``'no-bridge'``      — the agent ships no status bridge;
      * ``'missing-source'`` — a manifest source file is absent from the app tree
                               (can't install → button offers install but warns);
      * ``'current'``        — every file present, content-identical, exec bits
                               correct (button: "Bridge installed ✓" / "Reinstall");
      * ``'stale'``          — at least one file missing/different/non-executable
                               (button: "Update bridge" or "Install bridge").

    Defensive: an unreadable dest is treated as stale (re-install will repair);
    never raises.
    """
    import os as _os
    if home is None:
        home = _os.path.expanduser('~')
    manifest = _BRIDGE_MANIFEST.get(agent_id)
    if not manifest:
        return 'no-bridge'
    any_present = False
    all_current = True
    for spec in manifest:
        src = _os.path.join(app_dir, 'bridges', *spec['src'])
        if not _os.path.exists(src):
            return 'missing-source'
        try:
            with open(src, 'rb') as f:
                content = f.read()
            transform = spec.get('transform')
            if transform is not None:
                content = transform(content.decode('utf-8'), home).encode('utf-8')
        except OSError:
            return 'missing-source'
        dest = _os.path.join(home, spec['dest'])
        if not _os.path.exists(dest):
            all_current = False
            continue
        any_present = True
        try:
            with open(dest, 'rb') as f:
                if f.read() != content:
                    all_current = False
                    continue
        except OSError:
            all_current = False
            continue
        if spec.get('executable'):
            try:
                if not (_os.stat(dest).st_mode & 0o111):
                    all_current = False
            except OSError:
                all_current = False
    if all_current and any_present:
        return 'current'
    return 'stale'


def bridge_button_labels(state):
    """Map a ``bridge_state`` result to (button label, row subtitle) (M-UX.8).

    Pure presentation so the three labels are unit-pinnable without GTK:

      * ``current``        → ("Reinstall",      "Bridge installed ✓")
      * ``stale``          → ("Update bridge",  "Some files missing or out of date")
      * ``missing-source`` → ("Install bridge", "Bridge source not found in the app directory")
      * ``no-bridge``/else → ("Install bridge", "This agent ships a status bridge")
    """
    if state == 'current':
        return ('Reinstall', 'Bridge installed ✓')
    if state == 'stale':
        return ('Update bridge', 'Some files are missing or out of date')
    if state == 'missing-source':
        return ('Install bridge', 'Bridge source not found in the app directory')
    return ('Install bridge', 'Install the status bridge plugin')


def agent_doctor(settings, agent_id, *, run_fn=None):
    """Doctor-lite: resolve the agent's binary and run ``<binary> --version``.

    Returns ``(ok: bool, detail: str)``. ``ok`` is True when the binary runs and
    exits 0; ``detail`` is the first stdout line (the version) or an error
    summary. ``run_fn(argv) -> (rc, stdout)`` is injectable for tests; the
    default shells out with a short timeout and never raises. Full doctor
    (sessions dir, bridge presence) is P3.
    """
    adapter = ADAPTERS.get(agent_id)
    # Resolve the binary the same way the adapter would.
    if agent_id == 'claude':
        binary = settings.resolved_claude_binary
    elif adapter is not None and hasattr(adapter, '_binary'):
        binary = adapter._binary(settings)
    else:
        binary = agent_id
    if run_fn is None:
        def run_fn(argv):
            import subprocess
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=5, stdin=subprocess.DEVNULL)
                return (r.returncode, r.stdout)
            except (OSError, subprocess.SubprocessError):
                return None
    result = run_fn([binary, '--version'])
    if result is None:
        return (False, f'{binary}: not found or not runnable')
    rc, out = result
    first = (out or '').strip().splitlines()
    detail = first[0] if first else ''
    if rc == 0:
        return (True, detail or 'ok')
    return (False, detail or f'{binary}: exited {rc}')


def fallback_adapter(settings=None):
    """The adapter to use when a requested agent isn't available (M-P3.2).

    The Claude-less promise must hold even when the requested agent is missing,
    so the fallback is NOT hardcoded to claude. Resolution order:

      1. ``settings.agent_default`` if it is a registered adapter — the user's
         chosen default is the natural fallback;
      2. otherwise the first registered adapter in insertion order — so a fleet
         with claude removed still resolves to *something* real;
      3. only if the registry is somehow empty, ``ADAPTERS[DEFAULT_AGENT]`` (the
         legacy claude default) as a last resort.

    ``settings`` is optional: without it (or with an unregistered/blank default)
    the order degrades to "first-available", which on the shipped registry is
    claude — so callers that pass no settings keep today's behavior.
    """
    if settings is not None:
        default_id = getattr(settings, 'agent_default', '') or ''
        if default_id and default_id in ADAPTERS:
            return ADAPTERS[default_id]
    for adapter in ADAPTERS.values():
        return adapter
    return ADAPTERS[DEFAULT_AGENT]


def resolve_adapter(agent_id, settings=None):
    """Resolve ``agent_id`` to ``(adapter, missing_name)`` (A6/m3, M-P3.2).

    Distinguishes default-resolution from named-but-missing so the UI can warn:

      * known id (incl. an explicit ``'claude'``) → ``(adapter, None)``
      * a falsy id (``''``/``None`` — "use the default") →
        ``(fallback_adapter(settings), None)``; no name was requested, so
        nothing is missing.
      * a non-empty id with no registered adapter →
        ``(fallback_adapter(settings), <that id>)``; ``missing_name`` is the
        unknown id the caller asked for, so window.py can show a one-shot
        "agent 'X' not available — using <actual fallback>" toast.

    The fallback is NO LONGER hardcoded to claude (M-P3.2): it follows
    ``settings.agent_default`` then first-available, so the returned adapter is
    the agent that will ACTUALLY run — the toast names it truthfully. The
    returned adapter is always usable; only the diagnostic differs from
    ``get_adapter``. ``settings`` is optional so legacy single-arg callers keep
    the first-available (claude, on the shipped registry) fallback.
    """
    if agent_id and agent_id in ADAPTERS:
        return ADAPTERS[agent_id], None
    if not agent_id:
        return fallback_adapter(settings), None
    return fallback_adapter(settings), agent_id
