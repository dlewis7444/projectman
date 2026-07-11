"""Golden characterization tests for the P1 HarnessAdapter seam.

These tests pin the OBSERVABLE behavior of the current Claude-coupled spawn
path *before* the refactor, then assert the new ``harnesses.py`` seam reproduces
it byte-for-byte. They are headless (no GTK import) and spawn no processes.

Two layers, intentionally:

  * ``*Characterization`` classes reproduce the exact strings the *current*
    code emits — and guard that the live source files still contain those
    byte-patterns, so the golden literals are provably captured from current
    code, not transcribed by hand. These pass against the unrefactored tree.

  * ``*Parity`` classes assert the new ``harnesses.py`` API yields the same
    golden literals. These fail until ``harnesses.py`` exists, then pass — the
    proof that the seam is behavior-preserving.

The env half of the spawn contract (provider injection) is already pinned by
``tests/test_build_spawn_env.py`` against ``models.build_spawn_env``; ``ClaudeAdapter.spawn_plan``
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
# These run green against the unrefactored tree (no harnesses.py needed).
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
        """The golden continue-wrapper trap/exec body moved into harnesses.py
        intact (the seam relocated the logic; it was not rewritten).

        During the test-first phase this guard asserted the body in terminal.py
        and passed against the unrefactored tree, proving the golden literals
        were captured from live source. Post-refactor the canonical home is
        harnesses.py's build_continue_wrapper, and terminal.py no longer carries
        the hardcoded expression.
        """
        src = _source('harnesses.py')
        assert "trap 'exit 143' TERM HUP;" in src
        assert '[ "$s" -gt 0 ] && [ "$s" -le 128 ] && exec' in src
        # terminal.py must no longer hardcode the wrapper (it goes through agents).
        assert "trap 'exit 143' TERM HUP;" not in _source('terminal.py')


class TestCurrentZellijWrapperCharacterization:
    """Pin the zellij wrapper script + its claude continue command across the
    relocation into harnesses.py."""

    def test_source_guard_wrapper_script_relocated(self):
        """The wrapper script's load-bearing env/flag lines live in harnesses.py;
        terminal.py no longer hardcodes the script body."""
        src = _source('harnesses.py')
        assert "REAL_SHELL=\"${ZELLIJ_REAL_SHELL:-/bin/bash}\"" in src
        assert 'INIT_FILE="${HOME}/.ProjectMan/.zellij-init-${ZELLIJ_SESSION_NAME}"' in src
        assert 'exec "$REAL_SHELL" "$@"' in src
        # terminal.py delegates to harnesses.build_zellij_wrapper_script() now.
        tsrc = _source('terminal.py')
        assert 'build_zellij_wrapper_script' in tsrc
        assert 'if rm "$INIT_FILE" 2>/dev/null; then' not in tsrc

    def test_golden_continue_cmd_is_current_hardcode(self):
        assert GOLDEN_ZELLIJ_CONTINUE_CMD == 'claude -c || claude'


# ---------------------------------------------------------------------------
# Layer 2: parity — assert harnesses.py reproduces the goldens.
# These import harnesses.py (pure, no GTK); they fail until it exists.
# ---------------------------------------------------------------------------


class TestBuildContinueWrapperParity:
    """``build_continue_wrapper(continue_argv, fresh_argv)`` reproduces the
    current bash trap/respawn wrapper byte-for-byte for claude's argvs."""

    def test_native(self):
        import harnesses
        argv = harnesses.build_continue_wrapper(['claude', '-c'], ['claude'])
        assert argv == GOLDEN_CONTINUE_NATIVE

    def test_custom_binary_quoted(self):
        import harnesses
        argv = harnesses.build_continue_wrapper([CUSTOM_BIN, '-c'], [CUSTOM_BIN])
        assert argv == GOLDEN_CONTINUE_CUSTOM

    def test_returns_plain_list(self):
        import harnesses
        argv = harnesses.build_continue_wrapper(['claude', '-c'], ['claude'])
        assert type(argv) is list
        assert argv[0] == 'bash' and argv[1] == '-c'


class TestClaudeAdapterSpawnPlanParity:
    """``ClaudeAdapter.spawn_plan`` folds the current argv logic + provider env."""

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
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'resume', session_id='abc123')
        assert plan.argv == GOLDEN_RESUME_NATIVE

    def test_fresh_native_argv(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'fresh')
        assert plan.argv == GOLDEN_FRESH_NATIVE

    def test_continue_native_argv(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'continue')
        assert plan.argv == GOLDEN_CONTINUE_NATIVE

    def test_continue_custom_binary_argv(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(binary=CUSTOM_BIN), self._project(), 'continue')
        assert plan.argv == GOLDEN_CONTINUE_CUSTOM

    def test_resume_custom_binary_argv(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(binary=CUSTOM_BIN), self._project(), 'resume',
                            session_id='abc123')
        assert plan.argv == GOLDEN_RESUME_CUSTOM

    # --- env parity: delegates to models.build_spawn_env (already golden in test_ccr) ---

    def _custom_provider_settings(self, **kw):
        from settings import Settings
        providers = {
            'ollama': {
                'name': 'Ollama', 'base_url': 'http://host:11434',
                'api_key': 'secret', 'models': ['qwen'],
            },
        }
        return Settings(providers=providers, model_default='ollama', **kw)


    def test_native_model_env_is_none(self, monkeypatch):
        """Native path: env None, no fallback."""
        import harnesses
        a = harnesses.get_adapter('claude')
        plan = a.spawn_plan(self._settings(), self._project(), 'continue')
        assert plan.env is None
        assert plan.fallback_reason is None

    def test_custom_provider_injects_env(self, monkeypatch):
        """Custom provider → env carries ANTHROPIC_* from build_spawn_env."""
        import harnesses
        a = harnesses.ClaudeAdapter()
        s = self._custom_provider_settings()
        plan = a.spawn_plan(s, self._project('/projects/m'), 'fresh')
        assert plan.fallback_reason is None
        assert plan.env is not None
        assert plan.env['ANTHROPIC_BASE_URL'] == 'http://host:11434'
        assert plan.env['ANTHROPIC_AUTH_TOKEN'] == 'secret'

    def test_custom_provider_missing_base_url_falls_back_native(self):
        """Provider without base_url → env None + fallback_reason."""
        import harnesses
        from settings import Settings
        a = harnesses.ClaudeAdapter()
        s = Settings(providers={
            'ollama': {'name': 'O', 'base_url': '', 'api_key': 'k', 'models': ['q']},
        }, model_default='ollama')
        plan = a.spawn_plan(s, self._project('/projects/myproj'), 'continue')
        assert plan.env is None
        assert isinstance(plan.fallback_reason, str) and plan.fallback_reason


class TestZellijContinueCommandParity:
    """The flag-file content + new wrapper script for the zellij path."""

    def test_claude_continue_command_byte_identical(self):
        import harnesses
        cmd = harnesses.build_zellij_continue_command(['claude', '-c'], ['claude'])
        assert cmd == GOLDEN_ZELLIJ_CONTINUE_CMD

    def test_claude_continue_command_custom_binary(self):
        import harnesses
        cmd = harnesses.build_zellij_continue_command([CUSTOM_BIN, '-c'], [CUSTOM_BIN])
        assert cmd == "'/opt/my claude/claude' -c || '/opt/my claude/claude'"

    def test_adapter_exposes_zellij_continue_command(self):
        """ClaudeAdapter yields the claude continue command for the flag file."""
        import harnesses
        from settings import Settings
        a = harnesses.get_adapter('claude')
        assert a.zellij_continue_command(Settings()) == GOLDEN_ZELLIJ_CONTINUE_CMD

    def test_new_wrapper_execs_flag_content(self):
        """The new wrapper script must read+remove the flag and exec its
        content (not hardcode claude). Behavior parity is proven separately;
        here we pin the structural contract of the generalized script."""
        import harnesses
        script = harnesses.build_zellij_wrapper_script()
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
        import harnesses
        assert harnesses.build_zellij_wrapper_script() == GOLDEN_ZELLIJ_WRAPPER_NEW

    def test_new_wrapper_plus_flag_reconstruct_old_hardcode(self):
        """The line the new wrapper runs is ``eval "<flag content>"``; with the
        claude flag content that is ``eval "claude -c || claude"`` — exactly the
        old wrapper's hardcoded body. This is the equivalence, at string level.
        """
        import harnesses
        flag_content = harnesses.build_zellij_continue_command(['claude', '-c'], ['claude'])
        assert flag_content == 'claude -c || claude'
        # The old wrapper ran this literal line:
        assert 'claude -c || claude' in GOLDEN_ZELLIJ_WRAPPER_OLD
        # The new wrapper runs `eval "$CMD"` where $CMD is that same string.
        assert 'eval "$CMD"' in harnesses.build_zellij_wrapper_script()

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


class TestHarnessCapsAndRefs:
    """Dataclass contracts: ClaudeAdapter advertises full capabilities."""

    def test_claude_caps_all_true(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        caps = a.caps
        assert caps.continue_ is True
        assert caps.resume_by_id is True
        assert caps.sessions is True
        assert caps.rich_status is True
        assert caps.model_select is True
        assert caps.headless_json is True

    def test_claude_identity(self):
        import harnesses
        a = harnesses.get_adapter('claude')
        assert a.id == 'claude'
        assert a.display_name == 'Claude Code'

    def test_registry_has_claude(self):
        import harnesses
        assert 'claude' in harnesses.ADAPTERS
        assert harnesses.get_adapter('claude') is not None

    def test_registry_unknown_defaults_to_claude(self):
        """Unknown agent id resolves to the claude adapter (safe default)."""
        import harnesses
        a = harnesses.get_adapter('no-such-agent')
        assert a.id == 'claude'

    def test_session_ref_shape(self):
        import harnesses
        ref = harnesses.SessionRef(id='s1', title='Hello', last_active=123)
        assert ref.id == 's1'
        assert ref.title == 'Hello'
        assert ref.last_active == 123

    def test_spawn_plan_shape(self):
        import harnesses
        plan = harnesses.SpawnPlan(argv=['claude'], env=None, fallback_reason=None)
        assert plan.argv == ['claude']
        assert plan.env is None
        assert plan.fallback_reason is None


class TestClaudeAdapterListSessions:
    """``list_sessions`` delegates to HistoryReader and returns SessionRefs."""

    def test_delegates_to_history_reader(self, monkeypatch, tmp_path):
        import types
        import harnesses
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

        a = harnesses.get_adapter('claude')
        proj = types.SimpleNamespace(name='proj', path=proj_path)
        refs = a.list_sessions(proj)
        assert [r.id for r in refs] == ['s2', 's1'] or [r.id for r in refs] == ['s1', 's2']
        # newest-first ordering (s1 last_active=200 > s2=150)
        assert refs[0].id == 's1'
        assert refs[0].last_active == 200
        assert refs[0].title == 'First'
        assert all(isinstance(r, harnesses.SessionRef) for r in refs)


# ===========================================================================
# P2 Part A — seam hardening (A1-A6). Headless; no GTK, no processes.
# ===========================================================================

def _settings(**kw):
    from settings import Settings
    return Settings(**kw)


def _project(path='/projects/p'):
    import types
    return types.SimpleNamespace(name=os.path.basename(path), path=path)


def _custom_provider_settings(**kw):
    return Settings(providers={
        'ollama': {'name': 'O', 'base_url': 'http://host:11434',
                   'api_key': 'secret', 'models': ['qwen']},
    }, model_default='ollama', **kw)



class TestSpawnPlanUniformSignature:
    """A4/m1: ``spawn_plan(settings, project, mode, session_id=None)`` — no ccr
    test-injection on the protocol signature; it is a Claude-internal ctor param.
    """

    def test_spawn_plan_signature_has_no_ccr_kwargs(self):
        import inspect
        import harnesses
        params = list(inspect.signature(harnesses.ClaudeAdapter.spawn_plan).parameters)
        assert params == ['self', 'settings', 'project', 'mode', 'session_id']

    def test_claude_adapter_has_no_ccr_kwargs(self):
        import harnesses
        assert not hasattr(harnesses.get_adapter('claude'), '_ccr_kwargs')



class TestZellijSpawnEnvUnderAdapter:
    """A3/M3: ``zellij_spawn_env`` moves the provider env decision behind the adapter.

    terminal.py must consult only this method (no _claude_env, no hardcoded
    ANTHROPIC key list) — guarded by a source check below.
    """

    def test_native_model_returns_none_env(self):
        import harnesses
        a = harnesses.ClaudeAdapter()
        env, reason = a.zellij_spawn_env(_settings(), _project())
        assert env is None
        assert reason is None

    def test_custom_provider_returns_full_env(self):
        import harnesses
        from settings import Settings
        a = harnesses.ClaudeAdapter()
        s = Settings(providers={
            'ollama': {'name': 'O', 'base_url': 'http://h', 'api_key': 'k',
                       'models': ['q']}}, model_default='ollama')
        env, reason = a.zellij_spawn_env(s, _project('/p'))
        assert reason is None
        assert env is not None
        assert env['ANTHROPIC_BASE_URL'] == 'http://h'

    def test_custom_provider_missing_returns_reason(self):
        import harnesses
        from settings import Settings
        a = harnesses.ClaudeAdapter()
        s = Settings(providers={
            'ollama': {'name': 'O', 'base_url': '', 'api_key': 'k',
                       'models': ['q']}}, model_default='ollama')
        env, reason = a.zellij_spawn_env(s, _project('/p'))
        assert env is None
        assert reason

    def test_terminal_consults_adapter_not_hardcoded_env_for_zellij(self):
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
        import harnesses
        adapter, miss = harnesses.resolve_adapter('claude')
        assert adapter.id == 'claude'
        assert miss is None

    def test_empty_id_is_default_not_miss(self):
        """A falsy id means 'use the default' — nothing was named, nothing
        missing."""
        import harnesses
        for empty in ('', None):
            adapter, miss = harnesses.resolve_adapter(empty)
            assert adapter.id == 'claude'
            assert miss is None

    def test_unknown_named_agent_reports_miss(self):
        import harnesses
        adapter, miss = harnesses.resolve_adapter('codex')
        assert adapter.id == 'claude'   # safe fallback, still usable
        assert miss == 'codex'          # the name the caller asked for

    def test_get_adapter_still_hides_the_miss(self):
        """get_adapter keeps its safe-default contract (spawn path never breaks)."""
        import harnesses
        assert harnesses.get_adapter('codex').id == 'claude'


# A fake low-caps adapter exercises the caps-gating WIRING (A5) without needing
# a real second agent. It is the headless half of the gating contract; the GTK
# sliver (menu items appearing/disappearing) is covered in test_sidebar_state.py.
class _FakeLowCapsAdapter:
    id = 'fake-low'
    display_name = 'Fake (low caps)'

    def __init__(self):
        import harnesses
        self.caps = harnesses.HarnessCaps(
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
        import harnesses
        caps = harnesses.get_adapter('claude').caps
        assert caps.model_select and caps.sessions and caps.resume_by_id

    def test_low_caps_adapter_disables_gated_features(self):
        a = _FakeLowCapsAdapter()
        assert a.caps.model_select is False   # → Provider submenu hidden
        assert a.caps.sessions is False        # → expander hidden
        assert a.caps.resume_by_id is False    # → no resume rows


# ===========================================================================
# P3 Part A — mandate hardening. Each block pins one fresh-review mandate.
# ===========================================================================

# --- A1 / M-P3.2: unknown-agent fallback must never hardcode claude ----------

@pytest.fixture
def _registry_snapshot():
    """Save/restore ``harnesses.ADAPTERS`` so a test can model a different fleet
    (e.g. claude removed) without leaking into the rest of the suite."""
    import harnesses
    saved = dict(harnesses.ADAPTERS)
    yield harnesses.ADAPTERS
    harnesses.ADAPTERS.clear()
    harnesses.ADAPTERS.update(saved)


class TestUnknownAgentFallbackNotHardcodedClaude:
    """M-P3.2: a named-but-missing agent falls back to ``harness_default`` first,
    then first-available — NEVER a hardcoded claude. The toast/diagnostic and
    the spawn path must both name the harness that will ACTUALLY run, so the
    Claude-less promise holds even with a stale/bogus override."""

    def test_a1a_default_opencode_bogus_override_resolves_opencode(self):
        """T-A1a: harness_default=opencode + override='bogus' → the resolved
        adapter is opencode (the spawn path too), and the diagnostic names
        opencode — not claude."""
        import harnesses
        from settings import Settings
        s = Settings(harness_default='opencode',
                     harness_overrides={'/p': 'bogus'})
        effective = s.effective_harness('/p')
        assert effective == 'bogus'
        adapter, missing = harnesses.resolve_adapter(effective, s)
        assert adapter.id == 'opencode'          # the ACTUAL fallback
        assert adapter.display_name == 'OpenCode'  # the toast names OpenCode
        assert missing == 'bogus'                # the dead id, for the warning
        # The spawn path (get_adapter, what TerminalView resolves) agrees:
        assert harnesses.get_adapter(effective, s).id == 'opencode'

    def test_a1b_both_bogus_uses_first_available_not_claude(self,
                                                            _registry_snapshot):
        """T-A1b: harness_default ALSO bogus → first-available registered adapter.
        Proven against a claude-LESS fleet so 'first-available' is provably the
        mechanism, not 'happens to be claude'."""
        import harnesses
        from settings import Settings
        # Model a fleet with claude removed entirely; opencode is first.
        opencode = _registry_snapshot['opencode']
        _registry_snapshot.clear()
        _registry_snapshot['opencode'] = opencode
        s = Settings(harness_default='alsobogus',
                     harness_overrides={'/p': 'bogus'})
        adapter, missing = harnesses.resolve_adapter(s.effective_harness('/p'), s)
        assert adapter.id == 'opencode'   # first-available, NOT claude
        assert missing == 'bogus'
        assert harnesses.get_adapter('bogus', s).id == 'opencode'

    def test_a1b_registered_default_wins_over_first_available(self,
                                                              _registry_snapshot):
        """A registered ``harness_default`` beats first-available even when it is
        NOT the first key — the order is default-first, then first-available."""
        import harnesses
        from settings import Settings
        # Re-order so opencode is first and claude second.
        claude = _registry_snapshot['claude']
        opencode = _registry_snapshot['opencode']
        _registry_snapshot.clear()
        _registry_snapshot['opencode'] = opencode
        _registry_snapshot['claude'] = claude
        s = Settings(harness_default='claude')
        adapter, _ = harnesses.resolve_adapter('bogus', s)
        assert adapter.id == 'claude'   # default wins over the first key

    def test_a1c_default_claude_unchanged(self):
        """T-A1c regression: an all-claude fleet still resolves to claude with
        no diagnostic — the common path is untouched."""
        import harnesses
        from settings import Settings
        s = Settings(harness_default='claude')
        adapter, missing = harnesses.resolve_adapter(s.effective_harness('/p'), s)
        assert adapter.id == 'claude'
        assert missing is None
        assert harnesses.get_adapter('claude', s).id == 'claude'

    def test_a1c_get_adapter_no_settings_still_hides_miss_as_claude(self):
        """The legacy single-arg ``get_adapter`` contract is preserved: with no
        settings a miss falls back to claude (spawn path never breaks)."""
        import harnesses
        assert harnesses.get_adapter('codex').id == 'claude'

    def test_a1_fallback_adapter_helper_order(self, _registry_snapshot):
        """The helper itself: registered default wins; else first-available;
        a blank/unregistered default falls through to first-available."""
        import harnesses
        from settings import Settings
        # Registered default wins.
        assert harnesses.fallback_adapter(
            Settings(harness_default='opencode')).id == 'opencode'
        # Unregistered default → first-available (claude, shipped order).
        assert harnesses.fallback_adapter(
            Settings(harness_default='nope')).id == 'claude'
        # No settings at all → first-available.
        assert harnesses.fallback_adapter(None).id == 'claude'


# --- A2 / M-P3.3: continue-fallback policy is adapter-owned ------------------

class _FakeNoFallbackAdapter:
    """A minimal adapter that declares continue does NOT fall back to fresh —
    the codex/grok-shaped case the wrapper used to assume away."""
    id = 'fake-nofb'
    display_name = 'Fake (no continue fallback)'

    def __init__(self):
        import harnesses
        self.caps = harnesses.HarnessCaps(
            continue_=True, continue_falls_back_to_fresh=False,
        )

    def continue_argv(self, settings, project=None):
        return ['fakeagent', 'resume', '--last']

    def fresh_argv(self, settings, project=None):
        return ['fakeagent']

    def zellij_continue_command(self, settings, project=None):
        import harnesses
        return harnesses.build_zellij_continue_command(
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
        launch a fresh harness)."""
        import harnesses
        cmd = _FakeNoFallbackAdapter().zellij_continue_command(None)
        assert '||' not in cmd
        assert cmd == 'fakeagent resume --last'

    def test_a2a_no_fallback_builder_wrapper_has_no_exec_fresh(self):
        """The direct-spawn wrapper for a no-fallback adapter runs the continue
        command under the signal trap and never exec's fresh."""
        import harnesses
        argv = harnesses.build_continue_wrapper(
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
        import harnesses
        from settings import Settings
        a = harnesses.get_adapter('claude')
        assert a.zellij_continue_command(Settings()) == GOLDEN_ZELLIJ_CONTINUE_CMD
        assert a.zellij_continue_command(Settings()) == 'claude -c || claude'

    def test_a2b_claude_continue_wrapper_byte_identical(self):
        """T-A2b: claude's direct-spawn continue wrapper is byte-identical to
        the pre-refactor golden (the fresh-fallback tail intact)."""
        import harnesses
        from settings import Settings

        class _P:
            path = '/proj'
        plan = harnesses.get_adapter('claude').spawn_plan(Settings(), _P(),
                                                       'continue')
        assert plan.argv == GOLDEN_CONTINUE_NATIVE

    def test_a2b_opencode_zellij_command_byte_identical(self):
        """T-A2b: opencode's zellij continue command keeps the ``|| <fresh>``
        tail (it folds the model into both halves) — unchanged by the policy
        seam since opencode declares the fallback True."""
        import harnesses
        from settings import Settings

        class _P:
            path = '/proj'
        a = harnesses.get_adapter('opencode')
        # No model set → bare opencode on both halves, with the fresh tail.
        assert a.zellij_continue_command(Settings(), _P()) == 'opencode -c || opencode'
        # Model set → folded into BOTH halves, tail preserved.
        s = Settings(harness_default='opencode',
                     model_pins={'/proj': 'ollama/qwen'})
        assert a.zellij_continue_command(s, _P()) == (
            'opencode -m ollama/qwen -c || opencode -m ollama/qwen')

    def test_a2b_opencode_continue_wrapper_unchanged(self):
        """T-A2b: opencode's direct continue wrapper keeps the fresh-fallback
        body (both halves carry the model)."""
        import harnesses
        from settings import Settings

        class _P:
            path = '/proj'
        s = Settings(harness_default='opencode',
                     model_pins={'/proj': 'ollama/qwen'})
        plan = harnesses.get_adapter('opencode').spawn_plan(s, _P(), 'continue')
        script = plan.argv[-1]
        # The fresh-fallback machinery is present (fallback=True).
        assert 's=$?' in script and '-le 128' in script
        assert 'opencode -m ollama/qwen -c' in script

    def test_a2_default_caps_keep_fallback_true(self):
        """The shipped adapters declare the fallback policy True (today's
        behavior); the new field defaults True so nothing else changes."""
        import harnesses
        assert harnesses.HarnessCaps().continue_falls_back_to_fresh is True
        assert harnesses.get_adapter('claude').caps.continue_falls_back_to_fresh is True
        assert harnesses.get_adapter('opencode').caps.continue_falls_back_to_fresh is True


# --- A3 / M-P3.5: duplicate/builtin adapter id collision guard ---------------

class TestRegisterAdapterCollisionGuard:
    """M-P3.5: ``register_adapter`` REFUSES an id that already exists — builtins
    win, no silent dict shadowing. A custom 'claude' must not replace
    ClaudeAdapter."""

    def test_a3a_registering_builtin_id_raises_and_builtin_survives(self,
                                                                    _registry_snapshot):
        """T-A3a: registering id 'claude' raises ValueError and the real
        ClaudeAdapter survives intact (not shadowed)."""
        import harnesses
        before = harnesses.ADAPTERS['claude']

        class _Imposter:
            id = 'claude'
            display_name = 'Not Claude'
        with pytest.raises(ValueError):
            harnesses.register_adapter(_Imposter())
        # The builtin is unchanged — no silent overwrite.
        assert harnesses.ADAPTERS['claude'] is before
        assert type(harnesses.ADAPTERS['claude']).__name__ == 'ClaudeAdapter'
        assert harnesses.get_adapter('claude').id == 'claude'

    def test_a3a_registering_opencode_id_also_refused(self, _registry_snapshot):
        """The guard covers every builtin, not just claude."""
        import harnesses
        before = harnesses.ADAPTERS['opencode']

        class _Imposter:
            id = 'opencode'
        with pytest.raises(ValueError):
            harnesses.register_adapter(_Imposter())
        assert harnesses.ADAPTERS['opencode'] is before

    def test_a3b_novel_id_registers_and_resolves(self, _registry_snapshot):
        """T-A3b: a novel id registers and then resolves through the seam."""
        import harnesses

        class _Novel:
            id = 'novel-agent'
            display_name = 'Novel Agent'
        returned = harnesses.register_adapter(_Novel())
        assert returned.id == 'novel-agent'
        assert 'novel-agent' in harnesses.ADAPTERS
        assert harnesses.get_adapter('novel-agent').id == 'novel-agent'
        adapter, missing = harnesses.resolve_adapter('novel-agent')
        assert adapter.id == 'novel-agent'
        assert missing is None

    def test_a3_duplicate_custom_id_also_refused(self, _registry_snapshot):
        """Two customs cannot fight over one id — the second is refused (the
        first registration wins)."""
        import harnesses

        class _First:
            id = 'dup-id'
            display_name = 'First'

        class _Second:
            id = 'dup-id'
            display_name = 'Second'
        harnesses.register_adapter(_First())
        with pytest.raises(ValueError):
            harnesses.register_adapter(_Second())
        assert harnesses.ADAPTERS['dup-id'].display_name == 'First'

    def test_a3_empty_id_refused(self, _registry_snapshot):
        """An adapter with no id is refused rather than registered under ''."""
        import harnesses

        class _Anon:
            id = ''
        with pytest.raises(ValueError):
            harnesses.register_adapter(_Anon())
        assert '' not in harnesses.ADAPTERS

    def test_a3_builtin_ids_constant_matches_shipped(self):
        """The builtins frozenset matches the shipped adapters (the source of
        'builtins win'). P3 added grok as the third builtin — registered in
        ADAPTERS at import, so BUILTIN_HARNESS_IDS = frozenset(ADAPTERS) picks it
        up automatically."""
        import harnesses
        assert harnesses.BUILTIN_HARNESS_IDS == frozenset({'claude', 'opencode', 'grok'})
        # Grok is genuinely a builtin → it can't be replaced by a custom adapter.
        assert 'grok' in harnesses.BUILTIN_HARNESS_IDS


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
        import harnesses
        assert harnesses.get_adapter('claude').caps.rich_status is True
        assert harnesses.get_adapter('opencode').caps.rich_status is True
