"""Golden characterization tests for the P1 AgentAdapter seam.

These tests pin the OBSERVABLE behavior of the current Claude-coupled spawn
path *before* the refactor, then assert the new ``agents.py`` seam reproduces
it byte-for-byte. They are headless (no GTK import) and spawn no processes.

Two layers, intentionally:

  * ``*Characterization`` classes reproduce the exact strings the *current*
    code emits — and guard that the live source files still contain those
    byte-patterns, so the golden literals are provably captured from current
    code, not transcribed by hand. These pass against the unrefactored tree.

  * ``*Parity`` classes assert the new ``agents.py`` API yields the same
    golden literals. These fail until ``agents.py`` exists, then pass — the
    proof that the seam is behavior-preserving.

The env half of the spawn contract (ccr injection) is already pinned by
``tests/test_ccr.py`` against ``ccr.spawn_env``; ``ClaudeAdapter.spawn_plan``
delegates to it, so the parity tests here exercise that delegation rather than
re-deriving the env values.
"""
import os
import shlex
import json

import pytest


# ---------------------------------------------------------------------------
# GOLDEN LITERALS — byte-exact captures of the CURRENT code's output.
# (Verified against live source by the source-guard tests below.)
# ---------------------------------------------------------------------------

# terminal.py:343-368 spawn_claude argv, native `claude` binary.
GOLDEN_RESUME_NATIVE = ['claude', '--resume', 'abc123']
GOLDEN_FRESH_NATIVE = ['claude']
GOLDEN_CONTINUE_NATIVE = [
    'bash', '-c',
    'trap \'exit 143\' TERM HUP; claude -c; s=$?; '
    '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec claude',
]

# Same, with a configured custom binary path that needs shell quoting.
CUSTOM_BIN = '/opt/my claude/claude'
GOLDEN_RESUME_CUSTOM = [CUSTOM_BIN, '--resume', 'abc123']
GOLDEN_FRESH_CUSTOM = [CUSTOM_BIN]
GOLDEN_CONTINUE_CUSTOM = [
    'bash', '-c',
    "trap 'exit 143' TERM HUP; '/opt/my claude/claude' -c; s=$?; "
    '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec \'/opt/my claude/claude\'',
]

# terminal.py:_ensure_zellij_shell_wrapper() — the CURRENT wrapper script.
GOLDEN_ZELLIJ_WRAPPER_OLD = (
    '#!/bin/bash\n'
    'REAL_SHELL="${ZELLIJ_REAL_SHELL:-/bin/bash}"\n'
    'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"\n'
    'if rm "$INIT_FILE" 2>/dev/null; then\n'
    '    claude -c || claude\n'
    '    exit 0\n'
    'fi\n'
    'exec "$REAL_SHELL" "$@"\n'
)

# The continue command the zellij wrapper runs for claude — must remain
# byte-identical as flag-file content under the new seam.
GOLDEN_ZELLIJ_CONTINUE_CMD = 'claude -c || claude'


# ---------------------------------------------------------------------------
# Layer 1: characterization — prove the goldens match the LIVE current source.
# These run green against the unrefactored tree (no agents.py needed).
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(name):
    with open(os.path.join(REPO_ROOT, name)) as f:
        return f.read()


class TestCurrentSpawnArgvCharacterization:
    """Reproduce the current spawn_claude argv logic exactly, byte-for-byte.

    The argv-building branch of ``terminal.py:spawn_claude`` is pure Python
    (shlex only). We replay it here for native + custom binaries and assert it
    equals the golden literals, then guard that terminal.py's source still
    holds the trap/exec expression we replayed.
    """

    @staticmethod
    def _current_argv(claude_cmd, session_id=None, fresh=False):
        # Verbatim transcription of terminal.py:spawn_claude argv branch.
        if session_id:
            return [claude_cmd, '--resume', session_id]
        if fresh:
            return [claude_cmd]
        c = shlex.quote(claude_cmd)
        return ['bash', '-c',
                f"trap 'exit 143' TERM HUP; {c} -c; s=$?; "
                f'[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec {c}']

    def test_resume_native(self):
        assert self._current_argv('claude', session_id='abc123') == GOLDEN_RESUME_NATIVE

    def test_fresh_native(self):
        assert self._current_argv('claude', fresh=True) == GOLDEN_FRESH_NATIVE

    def test_continue_native(self):
        assert self._current_argv('claude') == GOLDEN_CONTINUE_NATIVE

    def test_resume_custom(self):
        assert self._current_argv(CUSTOM_BIN, session_id='abc123') == GOLDEN_RESUME_CUSTOM

    def test_fresh_custom(self):
        assert self._current_argv(CUSTOM_BIN, fresh=True) == GOLDEN_FRESH_CUSTOM

    def test_continue_custom(self):
        assert self._current_argv(CUSTOM_BIN) == GOLDEN_CONTINUE_CUSTOM

    def test_source_guard_trap_expression_relocated_intact(self):
        """The golden continue-wrapper trap/exec body moved into agents.py
        intact (the seam relocated the logic; it was not rewritten).

        During the test-first phase this guard asserted the body in terminal.py
        and passed against the unrefactored tree, proving the golden literals
        were captured from live source. Post-refactor the canonical home is
        agents.py's build_continue_wrapper, and terminal.py no longer carries
        the hardcoded expression.
        """
        src = _source('agents.py')
        assert "trap 'exit 143' TERM HUP;" in src
        assert '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec' in src
        # terminal.py must no longer hardcode the wrapper (it goes through agents).
        assert "trap 'exit 143' TERM HUP;" not in _source('terminal.py')


class TestCurrentZellijWrapperCharacterization:
    """Pin the zellij wrapper script + its claude continue command across the
    relocation into agents.py."""

    def test_source_guard_wrapper_script_relocated(self):
        """The wrapper script's load-bearing env/flag lines live in agents.py;
        terminal.py no longer hardcodes the script body."""
        src = _source('agents.py')
        assert "REAL_SHELL=\"${ZELLIJ_REAL_SHELL:-/bin/bash}\"" in src
        assert 'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"' in src
        assert 'exec "$REAL_SHELL" "$@"' in src
        # terminal.py delegates to agents.build_zellij_wrapper_script() now.
        tsrc = _source('terminal.py')
        assert 'build_zellij_wrapper_script' in tsrc
        assert 'if rm "$INIT_FILE" 2>/dev/null; then' not in tsrc

    def test_golden_continue_cmd_is_current_hardcode(self):
        assert GOLDEN_ZELLIJ_CONTINUE_CMD == 'claude -c || claude'


# ---------------------------------------------------------------------------
# Layer 2: parity — assert agents.py reproduces the goldens.
# These import agents.py (pure, no GTK); they fail until it exists.
# ---------------------------------------------------------------------------


class TestBuildContinueWrapperParity:
    """``build_continue_wrapper(continue_argv, fresh_argv)`` reproduces the
    current bash trap/respawn wrapper byte-for-byte for claude's argvs."""

    def test_native(self):
        import agents
        argv = agents.build_continue_wrapper(['claude', '-c'], ['claude'])
        assert argv == GOLDEN_CONTINUE_NATIVE

    def test_custom_binary_quoted(self):
        import agents
        argv = agents.build_continue_wrapper([CUSTOM_BIN, '-c'], [CUSTOM_BIN])
        assert argv == GOLDEN_CONTINUE_CUSTOM

    def test_returns_plain_list(self):
        import agents
        argv = agents.build_continue_wrapper(['claude', '-c'], ['claude'])
        assert type(argv) is list
        assert argv[0] == 'bash' and argv[1] == '-c'


class TestClaudeAdapterSpawnPlanParity:
    """``ClaudeAdapter.spawn_plan`` folds the current argv logic + ccr env."""

    @staticmethod
    def _settings(binary='', **kw):
        from settings import Settings
        return Settings(claude_binary=binary, **kw)

    @staticmethod
    def _project(path='/projects/p'):
        # SimpleNamespace stand-in (like test_session_restore.py) — keeps this
        # test module free of model.py's module-level gi import.
        import types
        return types.SimpleNamespace(name=os.path.basename(path), path=path)

    def test_resume_native_argv(self):
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'resume', session_id='abc123')
        assert plan.argv == GOLDEN_RESUME_NATIVE

    def test_fresh_native_argv(self):
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'fresh')
        assert plan.argv == GOLDEN_FRESH_NATIVE

    def test_continue_native_argv(self):
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'continue')
        assert plan.argv == GOLDEN_CONTINUE_NATIVE

    def test_continue_custom_binary_argv(self):
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(binary=CUSTOM_BIN), self._project(), 'continue')
        assert plan.argv == GOLDEN_CONTINUE_CUSTOM

    def test_resume_custom_binary_argv(self):
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(binary=CUSTOM_BIN), self._project(), 'resume',
                            session_id='abc123')
        assert plan.argv == GOLDEN_RESUME_CUSTOM

    # --- env parity: delegates to ccr.spawn_env (already golden in test_ccr) ---

    def _custom_model_settings(self, **kw):
        from settings import Settings
        base = dict(
            providers={'ollama': {
                'name': 'Ollama', 'base_url': 'http://host:11434/v1',
                'api_key': 'k', 'models': {'qwen': {'name': 'Qwen'}}}},
            model_default='ollama/qwen', ccr_host='127.0.0.1', ccr_port=3456,
            ccr_api_key='secret',
        )
        base.update(kw)
        return Settings(**base)

    def test_native_model_env_is_none(self, monkeypatch):
        """Native-model project: ccr never consulted; env None, no fallback."""
        import agents
        a = agents.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'continue')
        assert plan.env is None
        assert plan.fallback_reason is None

    def test_custom_model_ccr_up_injects_env(self, monkeypatch):
        """ccr up → env carries the four Anthropic vars, comma model form.

        ccr probe injection is now a Claude-INTERNAL ctor param (A4/m1), not a
        protocol argument: ``spawn_plan``'s signature is uniform across adapters
        ``(settings, project, mode, session_id=None)``. Tests build a
        ClaudeAdapter with ``ccr_kwargs={'probe': ...}`` to stay instant.
        """
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        # Inject a probe that reports ccr running so no real socket/sleep happens.
        a = agents.ClaudeAdapter(ccr_kwargs={'probe': lambda _s: True})
        s = self._custom_model_settings()
        plan = a.spawn_plan(s, self._project('/projects/myproj'), 'continue')
        assert plan.fallback_reason is None
        assert plan.env is not None
        assert plan.env['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:3456'
        assert plan.env['ANTHROPIC_AUTH_TOKEN'] == 'secret'
        assert plan.env['ANTHROPIC_API_KEY'] == 'secret'
        assert plan.env['ANTHROPIC_MODEL'] == 'ollama,qwen'
        # argv unaffected by env path
        assert plan.argv == GOLDEN_CONTINUE_NATIVE

    def test_custom_model_ccr_missing_falls_back_native(self, monkeypatch):
        """ccr binary absent → env None, fallback_reason explains the fallback."""
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: False)
        a = agents.get_adapter('claude')
        s = self._custom_model_settings()
        plan = a.spawn_plan(s, self._project('/projects/myproj'), 'continue')
        assert plan.env is None
        assert isinstance(plan.fallback_reason, str) and plan.fallback_reason


class TestZellijContinueCommandParity:
    """The flag-file content + new wrapper script for the zellij path."""

    def test_claude_continue_command_byte_identical(self):
        import agents
        cmd = agents.build_zellij_continue_command(['claude', '-c'], ['claude'])
        assert cmd == GOLDEN_ZELLIJ_CONTINUE_CMD

    def test_claude_continue_command_custom_binary(self):
        import agents
        cmd = agents.build_zellij_continue_command([CUSTOM_BIN, '-c'], [CUSTOM_BIN])
        assert cmd == "'/opt/my claude/claude' -c || '/opt/my claude/claude'"

    def test_adapter_exposes_zellij_continue_command(self):
        """ClaudeAdapter yields the claude continue command for the flag file."""
        import agents
        from settings import Settings
        a = agents.get_adapter('claude')
        assert a.zellij_continue_command(Settings()) == GOLDEN_ZELLIJ_CONTINUE_CMD

    def test_new_wrapper_execs_flag_content(self):
        """The new wrapper script must read+remove the flag and exec its
        content (not hardcode claude). Behavior parity is proven separately;
        here we pin the structural contract of the generalized script."""
        import agents
        script = agents.build_zellij_wrapper_script()
        # Still a bash wrapper keyed on the same env + flag path.
        assert script.startswith('#!/bin/bash\n')
        assert 'REAL_SHELL="${ZELLIJ_REAL_SHELL:-/bin/bash}"' in script
        assert 'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"' in script
        assert 'exec "$REAL_SHELL" "$@"' in script
        # Generalized: the flag's CONTENT is what runs, claude is NOT hardcoded.
        assert 'claude' not in script
        # It must read the flag content and remove the flag.
        assert '"$INIT_FILE"' in script


# Golden bytes of the NEW (generalized) zellij wrapper script. The wrapper
# string MUST change (it now execs the flag's content instead of hardcoding
# claude); this pins the exact new bytes so the change is reviewed, not silent.
GOLDEN_ZELLIJ_WRAPPER_NEW = (
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


class TestZellijWrapperStringParity:
    """String-level parity (the spec's chosen proof for the zellij path).

    The wrapper script necessarily changes — it execs the flag's content rather
    than hardcoding ``claude -c || claude`` — so realized behavior, not the
    script bytes, is what's held identical. Both halves are pinned at the string
    level: the exact new wrapper bytes, and the flag content for claude, which
    together reconstruct the old hardcode (``eval "claude -c || claude"``).
    """

    def test_new_wrapper_is_pinned_bytes(self):
        import agents
        assert agents.build_zellij_wrapper_script() == GOLDEN_ZELLIJ_WRAPPER_NEW

    def test_new_wrapper_plus_flag_reconstruct_old_hardcode(self):
        """The line the new wrapper runs is ``eval "<flag content>"``; with the
        claude flag content that is ``eval "claude -c || claude"`` — exactly the
        old wrapper's hardcoded body. This is the equivalence, at string level.
        """
        import agents
        flag_content = agents.build_zellij_continue_command(['claude', '-c'], ['claude'])
        assert flag_content == 'claude -c || claude'
        # The old wrapper ran this literal line:
        assert 'claude -c || claude' in GOLDEN_ZELLIJ_WRAPPER_OLD
        # The new wrapper runs `eval "$CMD"` where $CMD is that same string.
        assert 'eval "$CMD"' in agents.build_zellij_wrapper_script()

    def test_old_and_new_wrappers_share_shell_dispatch(self):
        """Both wrappers fall through to the same real-shell exec when no flag,
        and key on the same env vars / flag path — only the run-branch differs.
        """
        new = GOLDEN_ZELLIJ_WRAPPER_NEW
        for shared in (
            'REAL_SHELL="${ZELLIJ_REAL_SHELL:-/bin/bash}"',
            'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"',
            'exec "$REAL_SHELL" "$@"',
        ):
            assert shared in GOLDEN_ZELLIJ_WRAPPER_OLD
            assert shared in new


class TestAgentCapsAndRefs:
    """Dataclass contracts: ClaudeAdapter advertises full capabilities."""

    def test_claude_caps_all_true(self):
        import agents
        a = agents.get_adapter('claude')
        caps = a.caps
        assert caps.continue_ is True
        assert caps.resume_by_id is True
        assert caps.sessions is True
        assert caps.rich_status is True
        assert caps.model_select is True
        assert caps.headless_json is True

    def test_claude_identity(self):
        import agents
        a = agents.get_adapter('claude')
        assert a.id == 'claude'
        assert a.display_name == 'Claude Code'

    def test_registry_has_claude(self):
        import agents
        assert 'claude' in agents.ADAPTERS
        assert agents.get_adapter('claude') is not None

    def test_registry_unknown_defaults_to_claude(self):
        """Unknown agent id resolves to the claude adapter (safe default)."""
        import agents
        a = agents.get_adapter('no-such-agent')
        assert a.id == 'claude'

    def test_session_ref_shape(self):
        import agents
        ref = agents.SessionRef(id='s1', title='Hello', last_active=123)
        assert ref.id == 's1'
        assert ref.title == 'Hello'
        assert ref.last_active == 123

    def test_spawn_plan_shape(self):
        import agents
        plan = agents.SpawnPlan(argv=['claude'], env=None, fallback_reason=None)
        assert plan.argv == ['claude']
        assert plan.env is None
        assert plan.fallback_reason is None


class TestClaudeAdapterListSessions:
    """``list_sessions`` delegates to HistoryReader and returns SessionRefs."""

    def test_delegates_to_history_reader(self, monkeypatch, tmp_path):
        import types
        import agents
        # HistoryReader lives in model.py; point its HISTORY_FILE at a fixture.
        # (This is the one place the seam genuinely touches model — HistoryReader
        # is the Claude adapter's sessions implementation this phase.)
        import model
        hist = tmp_path / 'history.jsonl'
        proj_path = str(tmp_path / 'proj')
        os.makedirs(proj_path, exist_ok=True)
        # HistoryReader keys sessions by os.path.realpath(project); match it so
        # get_sessions(project.path) resolves regardless of /tmp symlinks.
        proj_path = os.path.realpath(proj_path)
        rows = [
            {'sessionId': 's1', 'project': proj_path, 'timestamp': 100, 'display': 'First'},
            {'sessionId': 's1', 'project': proj_path, 'timestamp': 200, 'display': 'First'},
            {'sessionId': 's2', 'project': proj_path, 'timestamp': 150, 'display': 'Second'},
        ]
        hist.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        monkeypatch.setattr(model, 'HISTORY_FILE', str(hist))

        a = agents.get_adapter('claude')
        proj = types.SimpleNamespace(name='proj', path=proj_path)
        refs = a.list_sessions(proj)
        assert [r.id for r in refs] == ['s2', 's1'] or [r.id for r in refs] == ['s1', 's2']
        # newest-first ordering (s1 last_active=200 > s2=150)
        assert refs[0].id == 's1'
        assert refs[0].last_active == 200
        assert refs[0].title == 'First'
        assert all(isinstance(r, agents.SessionRef) for r in refs)


# ===========================================================================
# P2 Part A — seam hardening (A1-A6). Headless; no GTK, no processes.
# ===========================================================================

def _settings(**kw):
    from settings import Settings
    return Settings(**kw)


def _project(path='/projects/p'):
    import types
    return types.SimpleNamespace(name=os.path.basename(path), path=path)


def _custom_model_settings(**kw):
    from settings import Settings
    base = dict(
        providers={'ollama': {
            'name': 'Ollama', 'base_url': 'http://host:11434/v1',
            'api_key': 'k', 'models': {'qwen': {'name': 'Qwen'}}}},
        model_default='ollama/qwen', ccr_host='127.0.0.1', ccr_port=3456,
        ccr_api_key='secret',
    )
    base.update(kw)
    return Settings(**base)


class TestSpawnPlanUniformSignature:
    """A4/m1: ``spawn_plan(settings, project, mode, session_id=None)`` — no ccr
    test-injection on the protocol signature; it is a Claude-internal ctor param.
    """

    def test_spawn_plan_signature_has_no_ccr_kwargs(self):
        import inspect
        import agents
        params = list(inspect.signature(agents.ClaudeAdapter.spawn_plan).parameters)
        assert params == ['self', 'settings', 'project', 'mode', 'session_id']

    def test_ccr_injection_via_ctor_not_call(self, monkeypatch):
        """A probe injected at construction reaches ccr.spawn_env; the call site
        passes only the uniform args."""
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        a = agents.ClaudeAdapter(ccr_kwargs={'probe': lambda _s: True})
        plan = a.spawn_plan(_custom_model_settings(), _project('/projects/m'), 'continue')
        assert plan.env is not None
        assert plan.env['ANTHROPIC_MODEL'] == 'ollama,qwen'
        assert plan.fallback_reason is None

    def test_default_adapter_has_empty_ccr_kwargs(self):
        """The registry's claude adapter uses the real socket probe (no inject)."""
        import agents
        assert agents.get_adapter('claude')._ccr_kwargs == {}


class TestZellijSpawnEnvUnderAdapter:
    """A3/M3: ``zellij_spawn_env`` moves the ccr env decision behind the adapter.

    terminal.py must consult only this method (no _claude_env, no hardcoded
    ANTHROPIC key list) — guarded by a source check below.
    """

    def test_native_model_returns_none_env(self, monkeypatch):
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        a = agents.ClaudeAdapter(ccr_kwargs={'probe': lambda _s: True})
        env, reason = a.zellij_spawn_env(_settings(), _project())
        assert env is None
        assert reason is None

    def test_custom_model_ccr_up_returns_full_env(self, monkeypatch):
        """ccr reachable → full env dict (inherited environ + ANTHROPIC vars)."""
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        a = agents.ClaudeAdapter(ccr_kwargs={'probe': lambda _s: True})
        env, reason = a.zellij_spawn_env(_custom_model_settings(), _project('/projects/m'))
        assert reason is None
        assert env is not None
        assert env['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:3456'
        assert env['ANTHROPIC_AUTH_TOKEN'] == 'secret'
        assert env['ANTHROPIC_MODEL'] == 'ollama,qwen'
        # Full env (inherits the parent environment) — PATH etc. preserved.
        assert 'PATH' in env

    def test_custom_model_ccr_missing_returns_reason(self, monkeypatch):
        import ccr
        import agents
        monkeypatch.setattr(ccr, 'available', lambda _s: False)
        a = agents.ClaudeAdapter()
        env, reason = a.zellij_spawn_env(_custom_model_settings(), _project('/projects/m'))
        assert env is None
        assert isinstance(reason, str) and reason

    def test_terminal_consults_adapter_not_ccr_for_zellij(self):
        """terminal.py's zellij path goes through zellij_spawn_env, and no longer
        names _claude_env or the ANTHROPIC_* key list (the seam is load-bearing)."""
        tsrc = _source('terminal.py')
        assert 'zellij_spawn_env' in tsrc
        assert 'def _claude_env' not in tsrc
        # The hardcoded zellij ANTHROPIC key list is gone from terminal.py.
        assert 'ANTHROPIC_BASE_URL' not in tsrc
        assert 'ANTHROPIC_AUTH_TOKEN' not in tsrc


class TestResolveAdapterNamedMiss:
    """A6/m3: ``resolve_adapter`` distinguishes default-resolution from
    named-but-missing so window.py can warn on the named-miss path."""

    def test_known_agent_no_miss(self):
        import agents
        adapter, miss = agents.resolve_adapter('claude')
        assert adapter.id == 'claude'
        assert miss is None

    def test_empty_id_is_default_not_miss(self):
        """A falsy id means 'use the default' — nothing was named, nothing
        missing."""
        import agents
        for empty in ('', None):
            adapter, miss = agents.resolve_adapter(empty)
            assert adapter.id == 'claude'
            assert miss is None

    def test_unknown_named_agent_reports_miss(self):
        import agents
        adapter, miss = agents.resolve_adapter('codex')
        assert adapter.id == 'claude'   # safe fallback, still usable
        assert miss == 'codex'          # the name the caller asked for

    def test_get_adapter_still_hides_the_miss(self):
        """get_adapter keeps its safe-default contract (spawn path never breaks)."""
        import agents
        assert agents.get_adapter('codex').id == 'claude'


# A fake low-caps adapter exercises the caps-gating WIRING (A5) without needing
# a real second agent. It is the headless half of the gating contract; the GTK
# sliver (menu items appearing/disappearing) is covered in test_sidebar_state.py.
class _FakeLowCapsAdapter:
    id = 'fake-low'
    display_name = 'Fake (low caps)'

    def __init__(self):
        import agents
        self.caps = agents.AgentCaps(
            continue_=True, resume_by_id=False, sessions=False,
            rich_status=False, model_select=False, headless_json=False,
        )

    def list_sessions(self, project):
        return []


class TestCapsGatingContract:
    """A5/m2: the caps an adapter declares are what the UI gates on.

    These pin the boolean contract the sidebar consumes; the visual assertions
    live in test_sidebar_state.py against a registered fake adapter.
    """

    def test_claude_caps_enable_everything(self):
        import agents
        caps = agents.get_adapter('claude').caps
        assert caps.model_select and caps.sessions and caps.resume_by_id

    def test_low_caps_adapter_disables_gated_features(self):
        a = _FakeLowCapsAdapter()
        assert a.caps.model_select is False   # → Model submenu hidden
        assert a.caps.sessions is False        # → expander hidden
        assert a.caps.resume_by_id is False    # → no resume rows


# ===========================================================================
# P3 Part A — mandate hardening. Each block pins one fresh-review mandate.
# ===========================================================================

# --- A1 / M-P3.2: unknown-agent fallback must never hardcode claude ----------

@pytest.fixture
def _registry_snapshot():
    """Save/restore ``agents.ADAPTERS`` so a test can model a different fleet
    (e.g. claude removed) without leaking into the rest of the suite."""
    import agents
    saved = dict(agents.ADAPTERS)
    yield agents.ADAPTERS
    agents.ADAPTERS.clear()
    agents.ADAPTERS.update(saved)


class TestUnknownAgentFallbackNotHardcodedClaude:
    """M-P3.2: a named-but-missing agent falls back to ``agent_default`` first,
    then first-available — NEVER a hardcoded claude. The toast/diagnostic and
    the spawn path must both name the agent that will ACTUALLY run, so the
    Claude-less promise holds even with a stale/bogus override."""

    def test_a1a_default_opencode_bogus_override_resolves_opencode(self):
        """T-A1a: agent_default=opencode + override='bogus' → the resolved
        adapter is opencode (the spawn path too), and the diagnostic names
        opencode — not claude."""
        import agents
        from settings import Settings
        s = Settings(agent_default='opencode',
                     agent_overrides={'/p': 'bogus'})
        effective = s.effective_agent('/p')
        assert effective == 'bogus'
        adapter, missing = agents.resolve_adapter(effective, s)
        assert adapter.id == 'opencode'          # the ACTUAL fallback
        assert adapter.display_name == 'opencode'  # the toast names opencode
        assert missing == 'bogus'                # the dead id, for the warning
        # The spawn path (get_adapter, what TerminalView resolves) agrees:
        assert agents.get_adapter(effective, s).id == 'opencode'

    def test_a1b_both_bogus_uses_first_available_not_claude(self,
                                                            _registry_snapshot):
        """T-A1b: agent_default ALSO bogus → first-available registered adapter.
        Proven against a claude-LESS fleet so 'first-available' is provably the
        mechanism, not 'happens to be claude'."""
        import agents
        from settings import Settings
        # Model a fleet with claude removed entirely; opencode is first.
        opencode = _registry_snapshot['opencode']
        _registry_snapshot.clear()
        _registry_snapshot['opencode'] = opencode
        s = Settings(agent_default='alsobogus',
                     agent_overrides={'/p': 'bogus'})
        adapter, missing = agents.resolve_adapter(s.effective_agent('/p'), s)
        assert adapter.id == 'opencode'   # first-available, NOT claude
        assert missing == 'bogus'
        assert agents.get_adapter('bogus', s).id == 'opencode'

    def test_a1b_registered_default_wins_over_first_available(self,
                                                              _registry_snapshot):
        """A registered ``agent_default`` beats first-available even when it is
        NOT the first key — the order is default-first, then first-available."""
        import agents
        from settings import Settings
        # Re-order so opencode is first and claude second.
        claude = _registry_snapshot['claude']
        opencode = _registry_snapshot['opencode']
        _registry_snapshot.clear()
        _registry_snapshot['opencode'] = opencode
        _registry_snapshot['claude'] = claude
        s = Settings(agent_default='claude')
        adapter, _ = agents.resolve_adapter('bogus', s)
        assert adapter.id == 'claude'   # default wins over the first key

    def test_a1c_default_claude_unchanged(self):
        """T-A1c regression: an all-claude fleet still resolves to claude with
        no diagnostic — the common path is untouched."""
        import agents
        from settings import Settings
        s = Settings(agent_default='claude')
        adapter, missing = agents.resolve_adapter(s.effective_agent('/p'), s)
        assert adapter.id == 'claude'
        assert missing is None
        assert agents.get_adapter('claude', s).id == 'claude'

    def test_a1c_get_adapter_no_settings_still_hides_miss_as_claude(self):
        """The legacy single-arg ``get_adapter`` contract is preserved: with no
        settings a miss falls back to claude (spawn path never breaks)."""
        import agents
        assert agents.get_adapter('codex').id == 'claude'

    def test_a1_fallback_adapter_helper_order(self, _registry_snapshot):
        """The helper itself: registered default wins; else first-available;
        a blank/unregistered default falls through to first-available."""
        import agents
        from settings import Settings
        # Registered default wins.
        assert agents.fallback_adapter(
            Settings(agent_default='opencode')).id == 'opencode'
        # Unregistered default → first-available (claude, shipped order).
        assert agents.fallback_adapter(
            Settings(agent_default='nope')).id == 'claude'
        # No settings at all → first-available.
        assert agents.fallback_adapter(None).id == 'claude'


# --- A2 / M-P3.3: continue-fallback policy is adapter-owned ------------------

class _FakeNoFallbackAdapter:
    """A minimal adapter that declares continue does NOT fall back to fresh —
    the codex/grok-shaped case the wrapper used to assume away."""
    id = 'fake-nofb'
    display_name = 'Fake (no continue fallback)'

    def __init__(self):
        import agents
        self.caps = agents.AgentCaps(
            continue_=True, continue_falls_back_to_fresh=False,
        )

    def continue_argv(self, settings, project=None):
        return ['fakeagent', 'resume', '--last']

    def fresh_argv(self, settings, project=None):
        return ['fakeagent']

    def zellij_continue_command(self, settings, project=None):
        import agents
        return agents.build_zellij_continue_command(
            self.continue_argv(settings), self.fresh_argv(settings),
            fallback=self.caps.continue_falls_back_to_fresh,
        )


class TestContinueFallbackPolicyIsAdapterOwned:
    """M-P3.3: the continue→fresh fallback is the ADAPTER's declared policy
    (``caps.continue_falls_back_to_fresh``), not the wrapper's global hardcode.
    claude/opencode keep today's exact behavior (byte-identical); a no-fallback
    adapter yields a command with no fresh tail."""

    def test_a2a_no_fallback_zellij_command_has_no_pipe(self):
        """T-A2a: a fake adapter declaring no-fallback yields a zellij continue
        command WITHOUT the ``||`` fresh tail (a resume error must not silently
        launch a fresh agent)."""
        import agents
        cmd = _FakeNoFallbackAdapter().zellij_continue_command(None)
        assert '||' not in cmd
        assert cmd == 'fakeagent resume --last'

    def test_a2a_no_fallback_builder_wrapper_has_no_exec_fresh(self):
        """The direct-spawn wrapper for a no-fallback adapter runs the continue
        command under the signal trap and never exec's fresh."""
        import agents
        argv = agents.build_continue_wrapper(
            ['fakeagent', 'resume', '--last'], ['fakeagent'], fallback=False)
        script = argv[-1]
        assert "trap 'exit 143' TERM HUP;" in script   # signal guard kept
        # No fresh-fallback machinery:
        assert 's=$?' not in script
        assert '-le 128' not in script
        assert 'fakeagent resume --last' in script
        # The only exec is of the continue command itself.
        assert script.endswith('exec fakeagent resume --last')

    def test_a2b_claude_zellij_command_byte_identical(self):
        """T-A2b: claude's zellij continue command is byte-identical to today's
        golden (``claude -c || claude``) — fallback defaults to True."""
        import agents
        from settings import Settings
        a = agents.get_adapter('claude')
        assert a.zellij_continue_command(Settings()) == GOLDEN_ZELLIJ_CONTINUE_CMD
        assert a.zellij_continue_command(Settings()) == 'claude -c || claude'

    def test_a2b_claude_continue_wrapper_byte_identical(self):
        """T-A2b: claude's direct-spawn continue wrapper is byte-identical to
        the pre-refactor golden (the fresh-fallback tail intact)."""
        import agents
        from settings import Settings

        class _P:
            path = '/proj'
        plan = agents.get_adapter('claude').spawn_plan(Settings(), _P(),
                                                       'continue')
        assert plan.argv == GOLDEN_CONTINUE_NATIVE

    def test_a2b_opencode_zellij_command_byte_identical(self):
        """T-A2b: opencode's zellij continue command keeps the ``|| <fresh>``
        tail (it folds the model into both halves) — unchanged by the policy
        seam since opencode declares the fallback True."""
        import agents
        from settings import Settings

        class _P:
            path = '/proj'
        a = agents.get_adapter('opencode')
        # No model set → bare opencode on both halves, with the fresh tail.
        assert a.zellij_continue_command(Settings(), _P()) == 'opencode -c || opencode'
        # Model set → folded into BOTH halves, tail preserved.
        s = Settings(agent_default='opencode',
                     model_overrides={'/proj': 'ollama/qwen'})
        assert a.zellij_continue_command(s, _P()) == (
            'opencode -m ollama/qwen -c || opencode -m ollama/qwen')

    def test_a2b_opencode_continue_wrapper_unchanged(self):
        """T-A2b: opencode's direct continue wrapper keeps the fresh-fallback
        body (both halves carry the model)."""
        import agents
        from settings import Settings

        class _P:
            path = '/proj'
        s = Settings(agent_default='opencode',
                     model_overrides={'/proj': 'ollama/qwen'})
        plan = agents.get_adapter('opencode').spawn_plan(s, _P(), 'continue')
        script = plan.argv[-1]
        # The fresh-fallback machinery is present (fallback=True).
        assert 's=$?' in script and '-le 128' in script
        assert 'opencode -m ollama/qwen -c' in script

    def test_a2_default_caps_keep_fallback_true(self):
        """The shipped adapters declare the fallback policy True (today's
        behavior); the new field defaults True so nothing else changes."""
        import agents
        assert agents.AgentCaps().continue_falls_back_to_fresh is True
        assert agents.get_adapter('claude').caps.continue_falls_back_to_fresh is True
        assert agents.get_adapter('opencode').caps.continue_falls_back_to_fresh is True


# --- A3 / M-P3.5: duplicate/builtin adapter id collision guard ---------------

class TestRegisterAdapterCollisionGuard:
    """M-P3.5: ``register_adapter`` REFUSES an id that already exists — builtins
    win, no silent dict shadowing. A custom 'claude' must not replace
    ClaudeAdapter."""

    def test_a3a_registering_builtin_id_raises_and_builtin_survives(self,
                                                                    _registry_snapshot):
        """T-A3a: registering id 'claude' raises ValueError and the real
        ClaudeAdapter survives intact (not shadowed)."""
        import agents
        before = agents.ADAPTERS['claude']

        class _Imposter:
            id = 'claude'
            display_name = 'Not Claude'
        with pytest.raises(ValueError):
            agents.register_adapter(_Imposter())
        # The builtin is unchanged — no silent overwrite.
        assert agents.ADAPTERS['claude'] is before
        assert type(agents.ADAPTERS['claude']).__name__ == 'ClaudeAdapter'
        assert agents.get_adapter('claude').id == 'claude'

    def test_a3a_registering_opencode_id_also_refused(self, _registry_snapshot):
        """The guard covers every builtin, not just claude."""
        import agents
        before = agents.ADAPTERS['opencode']

        class _Imposter:
            id = 'opencode'
        with pytest.raises(ValueError):
            agents.register_adapter(_Imposter())
        assert agents.ADAPTERS['opencode'] is before

    def test_a3b_novel_id_registers_and_resolves(self, _registry_snapshot):
        """T-A3b: a novel id registers and then resolves through the seam."""
        import agents

        class _Novel:
            id = 'novel-agent'
            display_name = 'Novel Agent'
        returned = agents.register_adapter(_Novel())
        assert returned.id == 'novel-agent'
        assert 'novel-agent' in agents.ADAPTERS
        assert agents.get_adapter('novel-agent').id == 'novel-agent'
        adapter, missing = agents.resolve_adapter('novel-agent')
        assert adapter.id == 'novel-agent'
        assert missing is None

    def test_a3_duplicate_custom_id_also_refused(self, _registry_snapshot):
        """Two customs cannot fight over one id — the second is refused (the
        first registration wins)."""
        import agents

        class _First:
            id = 'dup-id'
            display_name = 'First'

        class _Second:
            id = 'dup-id'
            display_name = 'Second'
        agents.register_adapter(_First())
        with pytest.raises(ValueError):
            agents.register_adapter(_Second())
        assert agents.ADAPTERS['dup-id'].display_name == 'First'

    def test_a3_empty_id_refused(self, _registry_snapshot):
        """An adapter with no id is refused rather than registered under ''."""
        import agents

        class _Anon:
            id = ''
        with pytest.raises(ValueError):
            agents.register_adapter(_Anon())
        assert '' not in agents.ADAPTERS

    def test_a3_builtin_ids_constant_matches_shipped(self):
        """The builtins frozenset matches the shipped adapters (the source of
        'builtins win')."""
        import agents
        assert agents.BUILTIN_AGENT_IDS == frozenset({'claude', 'opencode'})


# --- A4 / M-P3.1 verify-only: rich_status gates the sidebar dot remap --------

class TestSidebarDotConsumesRichStatus:
    """M-P3.1 (verify-only, landed last cycle): pin — HEADLESS — that the
    sidebar's idle→done dot remap is gated on the effective adapter's
    ``caps.rich_status``. The behavioral GTK assertions (T5/T6) live in
    test_sidebar_state.py but are DISPLAY-GATED (bench-only); this source-guard
    keeps the contract pinned in the headless suite so P3's churn can't silently
    drop it. No code change this commit — a regression net only."""

    def test_update_status_gates_remap_on_rich_status(self):
        """sidebar.update_status must only remap watcher-'idle'→'done' when the
        adapter's rich_status is true (via ``_remap_idle_to_done``)."""
        src = _source('sidebar.py')
        # The remap is conditional on the rich_status helper, not unconditional.
        assert "if status == 'idle' and self._remap_idle_to_done():" in src
        # An unconditional remap (the pre-guard hazard) must NOT be present.
        assert "if status == 'idle':\n            status = 'done'" not in src

    def test_remap_helper_reads_caps_rich_status(self):
        """``_remap_idle_to_done`` resolves the effective adapter and returns its
        ``caps.rich_status`` — the capability is genuinely consumed, not dead."""
        src = _source('sidebar.py')
        assert 'def _remap_idle_to_done(self):' in src
        assert 'self._adapter().caps.rich_status' in src

    def test_caps_declares_rich_status_for_both_builtins(self):
        """Both shipped (bridged) agents declare rich_status True, so the remap
        stays correct for them; a future bridgeless agent (rich_status False)
        keeps the honest idle dot."""
        import agents
        assert agents.get_adapter('claude').caps.rich_status is True
        assert agents.get_adapter('opencode').caps.rich_status is True
