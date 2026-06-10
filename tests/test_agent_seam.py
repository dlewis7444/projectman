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
