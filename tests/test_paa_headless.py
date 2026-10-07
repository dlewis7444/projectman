"""Unit tests for PAA headless harness axis (Phase A package 1).

No live LLM, no GTK. Golden argv / resolve policy / failure mapping / chat_turn.
"""
import json
import os
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from harnesses import (
    ADAPTERS,
    ClaudeAdapter,
    GrokAdapter,
    HarnessCaps,
    HeadlessPlan,
    HeadlessResult,
    KimiAdapter,
    OpencodeAdapter,
    adapter_implements_headless,
    register_adapter,
)
from paa_headless import (
    HeadlessHandle,
    chat_turn,
    claude_binary_resolvable,
    resolve_headless_adapter,
    run_headless,
    run_headless_cancellable,
)
from paa_haiku import _run_scan_model, _run_haiku
from settings import Settings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_claude_json(result_text, input_tokens=50, output_tokens=100,
                      session_id=None):
    data = {
        'type': 'result', 'subtype': 'success', 'is_error': False,
        'result': result_text,
        'usage': {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cache_read_input_tokens': 0,
            'cache_creation_input_tokens': 0,
        },
        'total_cost_usd': 0.001,
    }
    if session_id is not None:
        data['session_id'] = session_id
    return json.dumps(data)


def _ollama_providers(models=None):
    return {
        'ollama': {
            'name': 'Ollama',
            'base_url': 'http://localhost:11434',
            'api_key': 'secret-key',
            'models': models if models is not None else [
                'ministral-3', 'kimi-k2.7-code:cloud', 'glm-5.2:cloud',
            ],
        }
    }


@pytest.fixture
def _registry_snapshot():
    """Save/restore ADAPTERS so fake registrations do not leak."""
    import harnesses
    saved = dict(harnesses.ADAPTERS)
    yield harnesses.ADAPTERS
    harnesses.ADAPTERS.clear()
    harnesses.ADAPTERS.update(saved)


# ---------------------------------------------------------------------------
# W1 — Claude headless_plan golden argv
# ---------------------------------------------------------------------------

class TestClaudeHeadlessPlan:
    def test_stateless_scan_argv_native(self):
        """Byte-for-byte match of historical scan argv."""
        settings = Settings()
        adapter = ClaudeAdapter()
        plan = adapter.headless_plan(
            'scan prompt', settings, None, model='haiku')
        assert plan is not None
        bin_ = settings.resolved_claude_binary
        assert plan.argv == [
            bin_, '-p', '--model', 'haiku', '--output-format', 'json',
            'scan prompt',
        ]
        assert plan.env is None

    def test_stateless_scan_argv_with_project_path_native(self):
        settings = Settings(paa_scan_model='sonnet')
        adapter = ClaudeAdapter()
        plan = adapter.headless_plan(
            'p', settings, '/proj/foo', model='sonnet')
        assert plan.argv[plan.argv.index('--model') + 1] == 'sonnet'
        assert plan.env is None

    def test_custom_provider_injects_env_and_resolves_tier(self):
        settings = Settings(
            providers=_ollama_providers(),
            provider_defaults={'claude': 'ollama'},
            tier_models={'ollama': {
                'haiku': 'ministral-3',
                'sonnet': 'kimi-k2.7-code:cloud',
                'opus': 'glm-5.2:cloud',
            }},
        )
        adapter = ClaudeAdapter()
        plan = adapter.headless_plan(
            'scan me', settings, '/proj/foo', model='haiku')
        assert plan.argv[plan.argv.index('--model') + 1] == 'ministral-3'
        assert plan.env is not None
        assert plan.env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'
        assert plan.env['ANTHROPIC_AUTH_TOKEN'] == 'secret-key'
        assert plan.env['ANTHROPIC_API_KEY'] == ''

    def test_unusable_provider_falls_back_native(self):
        settings = Settings(
            providers={'bad': {'name': 'Bad', 'base_url': '', 'api_key': '',
                               'models': ['x']}},
            provider_defaults={'claude': 'bad'},
        )
        adapter = ClaudeAdapter()
        plan = adapter.headless_plan(
            'p', settings, '/proj/foo', model='haiku')
        assert plan.argv[plan.argv.index('--model') + 1] == 'haiku'
        assert plan.env is None

    def test_resume_argv_inserts_resume_flag(self):
        settings = Settings()
        adapter = ClaudeAdapter()
        plan = adapter.headless_plan(
            'follow up', settings, '', session_id='sess-abc', model='sonnet')
        assert '--resume' in plan.argv
        assert plan.argv[plan.argv.index('--resume') + 1] == 'sess-abc'
        # Still has -p, --model, --output-format, prompt
        assert plan.argv[0] == settings.resolved_claude_binary
        assert plan.argv[1] == '-p'
        assert '--output-format' in plan.argv
        assert plan.argv[-1] == 'follow up'

    def test_parse_headless_output_tokens_and_session(self):
        adapter = ClaudeAdapter()
        raw = _make_claude_json('hello', 10, 20, session_id='sid-1')
        result = adapter.parse_headless_output(raw)
        assert result.text == 'hello'
        assert result.tokens == 30
        assert result.session_id == 'sid-1'
        assert result.error is None

    def test_parse_headless_output_bad_json(self):
        adapter = ClaudeAdapter()
        result = adapter.parse_headless_output('not json')
        assert result.text is None
        assert result.tokens == 0
        assert result.error

    def test_claude_implements_headless(self):
        assert adapter_implements_headless(ADAPTERS['claude']) is True
        assert ADAPTERS['claude'].caps.headless_chat is True

    def test_non_claude_builtins_implement_headless_after_w5(self):
        for hid in ('opencode', 'grok', 'kimi'):
            a = ADAPTERS[hid]
            assert a.caps.headless_json is True
            assert a.caps.headless_chat is True
            assert adapter_implements_headless(a) is True
            plan = a.headless_plan('x', Settings(), '')
            assert plan is not None
            assert isinstance(plan.argv, list) and plan.argv


# ---------------------------------------------------------------------------
# W5 — non-claude headless golden argv + parse shapes (mocked; no live LLM)
# ---------------------------------------------------------------------------

class TestGrokHeadlessPlan:
    def test_stateless_argv(self):
        settings = Settings()
        adapter = GrokAdapter()
        plan = adapter.headless_plan('scan me', settings, '')
        assert plan is not None
        assert plan.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'
        assert plan.env['GROK_CURSOR_MCPS_ENABLED'] == '0'
        assert 'PAA_APPROVAL_SOCK' not in plan.env
        assert plan.argv[0] == 'grok'
        assert '-p' in plan.argv
        assert plan.argv[plan.argv.index('-p') + 1] == 'scan me'
        assert '--output-format' in plan.argv
        assert plan.argv[plan.argv.index('--output-format') + 1] == 'json'
        assert '-r' not in plan.argv

    def test_resume_and_model_argv(self):
        settings = Settings()
        # Pin a per-project model via harness axis... effective_model needs
        # model_pins / overrides. Pass model= explicitly (caller override).
        adapter = GrokAdapter()
        plan = adapter.headless_plan(
            'follow up', settings, '/proj',
            session_id='019f-sid', model='pool-qwen')
        assert plan.argv[plan.argv.index('-r') + 1] == '019f-sid'
        assert plan.argv[plan.argv.index('-m') + 1] == 'pool-qwen'
        assert plan.argv[plan.argv.index('-p') + 1] == 'follow up'

    def test_model_pins_yields_m_flag(self):
        """model_pins + model=None → ``-m <pin>`` via effective_model."""
        settings = Settings(model_pins={'/proj': 'grok-key'})
        adapter = GrokAdapter()
        plan = adapter.headless_plan('p', settings, '/proj', model=None)
        assert '-m' in plan.argv
        assert plan.argv[plan.argv.index('-m') + 1] == 'grok-key'

    def test_no_model_pin_omits_m(self):
        adapter = GrokAdapter()
        plan = adapter.headless_plan('p', Settings(), '')
        assert '-m' not in plan.argv

    def test_parse_shape(self):
        adapter = GrokAdapter()
        raw = json.dumps({
            'text': 'PONG',
            'sessionId': '019f-abc',
            'usage': {'input_tokens': 10, 'output_tokens': 5},
        })
        result = adapter.parse_headless_output(raw)
        assert result.text == 'PONG'
        assert result.tokens == 15
        assert result.session_id == '019f-abc'
        assert result.error is None

    def test_parse_bad_json(self):
        adapter = GrokAdapter()
        result = adapter.parse_headless_output('not json')
        assert result.text is None
        assert result.error


class TestOpencodeHeadlessPlan:
    def test_stateless_argv(self):
        settings = Settings()
        adapter = OpencodeAdapter()
        plan = adapter.headless_plan('scan me', settings, '')
        assert plan is not None
        assert plan.env is None
        assert plan.argv[:3] == ['opencode', 'run', 'scan me']
        assert '--format' in plan.argv
        assert plan.argv[plan.argv.index('--format') + 1] == 'json'
        assert '-s' not in plan.argv

    def test_resume_and_model_argv(self):
        adapter = OpencodeAdapter()
        plan = adapter.headless_plan(
            'more', Settings(), '',
            session_id='ses_abc', model='ollama/qwen3.5:cloud')
        assert plan.argv[plan.argv.index('-s') + 1] == 'ses_abc'
        assert plan.argv[plan.argv.index('-m') + 1] == 'ollama/qwen3.5:cloud'

    def test_custom_binary(self):
        settings = Settings(harnesses={'opencode': {'binary': '/opt/oc'}})
        adapter = OpencodeAdapter()
        plan = adapter.headless_plan('p', settings, '')
        assert plan.argv[0] == '/opt/oc'

    def test_parse_ndjson_shape(self):
        adapter = OpencodeAdapter()
        lines = [
            json.dumps({
                'type': 'step_start', 'sessionID': 'ses_1',
                'part': {'type': 'step-start'},
            }),
            json.dumps({
                'type': 'text', 'sessionID': 'ses_1',
                'part': {'type': 'text', 'text': 'HEL'},
            }),
            json.dumps({
                'type': 'text', 'sessionID': 'ses_1',
                'part': {'type': 'text', 'text': 'LO'},
            }),
            json.dumps({
                'type': 'step_finish', 'sessionID': 'ses_1',
                'part': {
                    'type': 'step-finish',
                    'tokens': {'input': 100, 'output': 7},
                },
            }),
        ]
        result = adapter.parse_headless_output('\n'.join(lines))
        assert result.text == 'HELLO'
        assert result.tokens == 107
        assert result.session_id == 'ses_1'
        assert result.error is None

    def test_parse_empty_unparseable(self):
        adapter = OpencodeAdapter()
        result = adapter.parse_headless_output('not json\nalso not')
        assert result.text is None
        assert result.error

    def test_parse_error_event_fails_even_with_session(self):
        """Error-only NDJSON must not look like empty success (W5 Should-fix #1)."""
        adapter = OpencodeAdapter()
        lines = [
            json.dumps({
                'type': 'step_start', 'sessionID': 'ses_err',
                'part': {'type': 'step-start'},
            }),
            json.dumps({
                'type': 'error', 'sessionID': 'ses_err',
                'error': {'message': 'model refused / tool failed'},
            }),
        ]
        result = adapter.parse_headless_output('\n'.join(lines))
        assert result.text is None
        assert result.error is not None
        assert 'tool failed' in result.error or 'model refused' in result.error
        # sessionID may still be present for diagnostics; failure is via error.
        assert result.session_id == 'ses_err'

    def test_parse_error_event_string_shape(self):
        adapter = OpencodeAdapter()
        raw = json.dumps({
            'type': 'error', 'sessionID': 'ses_2',
            'error': 'provider timeout',
        })
        result = adapter.parse_headless_output(raw)
        assert result.text is None
        assert result.error == 'provider timeout'

    def test_parse_error_with_partial_text_still_fails(self):
        """Conservative: any error event → fail even if some text was emitted."""
        adapter = OpencodeAdapter()
        lines = [
            json.dumps({
                'type': 'text', 'sessionID': 'ses_3',
                'part': {'type': 'text', 'text': 'partial'},
            }),
            json.dumps({
                'type': 'error', 'sessionID': 'ses_3',
                'error': {'message': 'aborted'},
            }),
        ]
        result = adapter.parse_headless_output('\n'.join(lines))
        assert result.text is None
        assert result.error == 'aborted'

    def test_model_pins_yields_m_flag(self):
        settings = Settings(model_pins={'/proj': 'ollama/qwen3.5:cloud'})
        adapter = OpencodeAdapter()
        plan = adapter.headless_plan('p', settings, '/proj', model=None)
        assert plan.argv[plan.argv.index('-m') + 1] == 'ollama/qwen3.5:cloud'


class TestKimiHeadlessPlan:
    def test_stateless_argv(self):
        adapter = KimiAdapter()
        plan = adapter.headless_plan('scan me', Settings(), '')
        assert plan is not None
        assert plan.env is None
        assert plan.argv[0] == 'kimi'
        assert plan.argv[plan.argv.index('-p') + 1] == 'scan me'
        assert plan.argv[plan.argv.index('--output-format') + 1] == 'stream-json'
        # Never combine -p with --yolo (CLI rejects it).
        assert '--yolo' not in plan.argv and '-y' not in plan.argv
        assert '-S' not in plan.argv

    def test_resume_and_model_argv(self):
        adapter = KimiAdapter()
        plan = adapter.headless_plan(
            'more', Settings(), '',
            session_id='session_deadbeef', model='kimi-code/k3')
        assert plan.argv[plan.argv.index('-S') + 1] == 'session_deadbeef'
        assert plan.argv[plan.argv.index('-m') + 1] == 'kimi-code/k3'

    def test_model_pins_yields_m_flag(self):
        settings = Settings(model_pins={'/proj': 'kimi-code/k3'})
        adapter = KimiAdapter()
        plan = adapter.headless_plan('p', settings, '/proj', model=None)
        assert plan.argv[plan.argv.index('-m') + 1] == 'kimi-code/k3'

    def test_parse_stream_json_shape(self):
        adapter = KimiAdapter()
        lines = [
            json.dumps({'role': 'assistant', 'content': 'PONG'}),
            json.dumps({
                'role': 'meta',
                'type': 'session.resume_hint',
                'session_id': 'session_abc',
                'command': 'kimi -r session_abc',
                'content': 'To resume…',
            }),
        ]
        result = adapter.parse_headless_output('\n'.join(lines))
        assert result.text == 'PONG'
        assert result.tokens == 0  # stream-json has no usage
        assert result.session_id == 'session_abc'
        assert result.error is None

    def test_parse_bad(self):
        adapter = KimiAdapter()
        result = adapter.parse_headless_output('')
        assert result.text is None
        assert result.error


# ---------------------------------------------------------------------------
# W2 — resolve_headless_adapter
# ---------------------------------------------------------------------------

class TestResolveHeadlessAdapter:
    def test_default_resolves_claude(self):
        settings = Settings()  # harness_default=claude
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert adapter is not None
        assert adapter.id == 'claude'
        assert hid == 'claude'
        assert reason is None

    def test_per_project_override_to_claude(self):
        settings = Settings(
            harness_default='opencode',
            harness_overrides={'/proj/a': 'claude'},
        )
        adapter, hid, reason = resolve_headless_adapter(settings, '/proj/a')
        assert adapter.id == 'claude'
        assert hid == 'claude'
        assert reason is None

    def test_fallback_when_no_headless_impl(self, _registry_snapshot):
        """Harness with headless_json=False falls back to claude."""
        class _NoHeadless:
            id = 'no-hl'
            display_name = 'No Headless'
            caps = HarnessCaps(headless_json=False)

        register_adapter(_NoHeadless())
        settings = Settings(harness_default='no-hl')
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'no-hl'
        assert adapter is not None
        assert adapter.id == 'claude'
        assert reason is not None
        assert 'claude' in reason
        assert 'headless' in reason.lower() or 'no-hl' in reason

    def test_fallback_when_stub_headless_plan(self, _registry_snapshot):
        """headless_json=True but stub plan → fallback (W5 deferral shape)."""
        from harnesses import _default_headless_plan, _default_parse_headless_output

        class _StubHL:
            id = 'stub-hl'
            display_name = 'Stub HL'
            caps = HarnessCaps(headless_json=True)
            headless_plan = _default_headless_plan
            parse_headless_output = _default_parse_headless_output

        register_adapter(_StubHL())
        settings = Settings(harness_default='stub-hl')
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'stub-hl'
        assert adapter.id == 'claude'
        assert 'no headless plan' in reason

    def test_resolves_builtin_opencode(self):
        """W5: opencode has a real headless plan — no claude fallback."""
        settings = Settings(harness_default='opencode')
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'opencode'
        assert adapter is not None
        assert adapter.id == 'opencode'
        assert reason is None

    def test_resolves_builtin_grok(self):
        settings = Settings(harness_default='grok')
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'grok'
        assert adapter is not None
        assert adapter.id == 'grok'
        assert reason is None

    def test_resolves_builtin_kimi(self):
        settings = Settings(harness_default='kimi')
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'kimi'
        assert adapter is not None
        assert adapter.id == 'kimi'
        assert reason is None

    def test_claude_missing_binary_returns_none_when_fallback_needed(
            self, _registry_snapshot):
        """When resolved harness cannot do headless, claude-missing → None."""
        class _NoHL:
            id = 'no-hl-miss'
            display_name = 'No HL'
            caps = HarnessCaps(headless_json=False)

        register_adapter(_NoHL())
        settings = Settings(
            harness_default='no-hl-miss',
            claude_binary='/nonexistent/claude-paa-test-xyz',
        )
        settings.harnesses = {}
        adapter, hid, reason = resolve_headless_adapter(settings, '')
        assert adapter is None
        assert hid == 'no-hl-miss'
        assert reason is not None
        assert 'unresolvable' in reason or 'unavailable' in reason


# ---------------------------------------------------------------------------
# W2 — run_headless failure mapping
# ---------------------------------------------------------------------------

class TestRunHeadless:
    def test_success(self):
        settings = Settings()
        plan = HeadlessPlan(
            argv=['true'], env=None, cwd=None, timeout=5)
        # Use a parse that ignores stdout
        def parse(_stdout):
            return HeadlessResult(text='ok', tokens=1)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{}')
            result = run_headless(plan, parse, settings=settings)
        assert result.text == 'ok'
        assert result.tokens == 1
        kwargs = mock_run.call_args.kwargs
        assert kwargs['capture_output'] is True
        assert kwargs['text'] is True
        assert kwargs['stdin'] is subprocess.DEVNULL
        assert kwargs['timeout'] == 5

    def test_timeout(self):
        plan = HeadlessPlan(argv=['x'], cwd='/tmp', timeout=1)
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('x', 1)):
            result = run_headless(plan, lambda s: HeadlessResult(text=s))
        assert result.text is None
        assert result.error == 'timeout'

    def test_not_found(self):
        plan = HeadlessPlan(argv=['x'], cwd='/tmp')
        with patch('subprocess.run', side_effect=FileNotFoundError('x')):
            result = run_headless(plan, lambda s: HeadlessResult(text=s))
        assert result.text is None
        assert result.error

    def test_nonzero_exit(self):
        plan = HeadlessPlan(argv=['x'], cwd='/tmp')
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=2, stdout='')):
            result = run_headless(plan, lambda s: HeadlessResult(text='x'))
        assert result.text is None
        assert 'exit 2' in result.error

    def test_bad_json_via_parse(self):
        plan = HeadlessPlan(argv=['x'], cwd='/tmp')
        adapter = ClaudeAdapter()
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='nope')):
            result = run_headless(plan, adapter.parse_headless_output)
        assert result.text is None
        assert result.error

    def test_none_plan(self):
        result = run_headless(None, lambda s: HeadlessResult(text=s))
        assert result.text is None

    def test_cwd_none_without_settings_refuses_process_cwd(self):
        """plan.cwd is None + no settings → clear error, not process cwd."""
        plan = HeadlessPlan(argv=['true'], cwd=None, timeout=5)
        with patch('subprocess.run') as mock_run:
            result = run_headless(plan, lambda s: HeadlessResult(text=s))
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        assert 'process cwd' in result.error or 'settings' in result.error


# ---------------------------------------------------------------------------
# W3 — _run_scan_model rewire parity with existing tests
# ---------------------------------------------------------------------------

class TestRunScanModelParity:
    def test_success_native(self):
        settings = Settings()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('hello world')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            text, tokens = _run_haiku('test prompt', settings)
        assert text == 'hello world'
        assert tokens == 150
        args = mock_run.call_args
        argv = args[0][0]
        assert argv[1] == '-p'
        assert '--output-format' in argv
        assert argv[argv.index('--model') + 1] == 'haiku'
        assert args.kwargs.get('env') is None

    def test_custom_provider(self):
        settings = Settings(
            providers=_ollama_providers(),
            provider_defaults={'claude': 'ollama'},
            paa_scan_model='haiku',
            tier_models={'ollama': {
                'haiku': 'ministral-3',
                'sonnet': 'kimi-k2.7-code:cloud',
                'opus': 'glm-5.2:cloud',
            }},
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('ok')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            text, tokens = _run_scan_model(
                'scan me', settings, project_path='/proj/foo')
        assert text == 'ok'
        assert tokens == 150
        argv = mock_run.call_args[0][0]
        assert argv[argv.index('--model') + 1] == 'ministral-3'
        env = mock_run.call_args.kwargs.get('env')
        assert env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'

    def test_timeout_returns_none_zero(self):
        settings = Settings()
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('claude', 30)):
            text, tokens = _run_haiku('test', settings)
        assert text is None
        assert tokens == 0

    def test_opencode_default_uses_opencode_run_argv(self):
        """W5: harness_default=opencode routes scans through opencode run."""
        settings = Settings(harness_default='opencode', paa_scan_model='haiku')
        # NDJSON shape from live probe; not claude JSON.
        oc_stdout = '\n'.join([
            json.dumps({
                'type': 'text', 'sessionID': 'ses_x',
                'part': {'type': 'text', 'text': 'via-opencode'},
            }),
            json.dumps({
                'type': 'step_finish', 'sessionID': 'ses_x',
                'part': {'tokens': {'input': 1, 'output': 2}},
            }),
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = oc_stdout
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            text, tokens = _run_scan_model('p', settings, project_path='/p')
        assert text == 'via-opencode'
        assert tokens == 3
        argv = mock_run.call_args[0][0]
        assert argv[0] == 'opencode'
        assert argv[1] == 'run'
        assert '--format' in argv
        # Tier aliases stay claude-only: no -m haiku injected on opencode path.
        if '-m' in argv:
            assert argv[argv.index('-m') + 1] not in (
                'haiku', 'sonnet', 'opus', 'fable', 'subagent')

    def test_cwd_ends_with_project_admin_agent(self, tmp_path):
        """Regression magnet: plan.cwd None + settings → isolated PAA cwd."""
        settings = Settings(projects_dir=str(tmp_path))
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('ok')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            text, tokens = _run_scan_model(
                'p', settings, project_path='/proj/foo')
        assert text == 'ok'
        cwd = mock_run.call_args.kwargs.get('cwd')
        assert cwd is not None
        assert cwd.endswith('.project-admin-agent')
        assert str(tmp_path) in cwd

    def test_headless_plan_none_falls_back_to_claude(self, _registry_snapshot):
        """Implements headless but plan returns None → claude, not silent fail."""
        class _NonePlan:
            id = 'none-plan'
            display_name = 'None Plan'
            caps = HarnessCaps(headless_json=True, headless_chat=False)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return None  # claims capability but cannot plan this call

            def parse_headless_output(self, stdout):
                return HeadlessResult(text=stdout, tokens=0)

        register_adapter(_NonePlan())
        settings = Settings(harness_default='none-plan', paa_scan_model='haiku')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('from-claude')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            text, tokens = _run_scan_model(
                'scan', settings, project_path='/p')
        assert text == 'from-claude'
        argv = mock_run.call_args[0][0]
        assert argv[1] == '-p'
        assert '--output-format' in argv
        assert argv[argv.index('--model') + 1] == 'haiku'


# ---------------------------------------------------------------------------
# W4 — chat_turn
# ---------------------------------------------------------------------------

class TestChatTurn:
    def test_new_session_returns_session_id(self):
        settings = Settings()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json(
            'hi there', session_id='new-sess-1')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = chat_turn(settings, 'hello', project_path='')
        assert result.text == 'hi there'
        assert result.session_id == 'new-sess-1'
        argv = mock_run.call_args[0][0]
        assert '--resume' not in argv
        assert '-p' in argv

    def test_resume_argv(self):
        settings = Settings()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json(
            'continued', session_id='sess-abc')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = chat_turn(
                settings, 'more', session_id='sess-abc', project_path='')
        assert result.text == 'continued'
        assert result.session_id == 'sess-abc'
        argv = mock_run.call_args[0][0]
        assert argv[argv.index('--resume') + 1] == 'sess-abc'

    def test_error_on_timeout(self):
        settings = Settings()
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('claude', 30)):
            result = chat_turn(settings, 'x')
        assert result.text is None
        assert result.error

    def test_nonzero_exit(self):
        settings = Settings()
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=3, stdout='')):
            result = chat_turn(settings, 'x')
        assert result.text is None
        assert result.error
        assert 'exit 3' in result.error

    def test_bad_json(self):
        settings = Settings()
        with patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='not-json')):
            result = chat_turn(settings, 'x')
        assert result.text is None
        assert result.error

    def test_resume_refused_without_headless_chat(self, _registry_snapshot):
        """Adapter with headless_json but no headless_chat cannot resume;
        falls back to claude when available."""
        from harnesses import HeadlessPlan as HP

        class _JsonOnly:
            id = 'json-only'
            display_name = 'JSON Only'
            caps = HarnessCaps(headless_json=True, headless_chat=False)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return HP(argv=['echo', prompt], timeout=5)

            def parse_headless_output(self, stdout):
                return HeadlessResult(text=stdout.strip(), tokens=0)

        register_adapter(_JsonOnly())
        settings = Settings(harness_default='json-only')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json(
            'via claude', session_id='s1')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = chat_turn(
                settings, 'resume me', session_id='old', project_path='')
        # Fallback to claude (has headless_chat)
        assert result.text == 'via claude'
        argv = mock_run.call_args[0][0]
        assert '--resume' in argv

    def test_resume_hard_refuse_when_claude_unresolvable(
            self, _registry_snapshot):
        """headless_chat=False + claude binary missing → hard refuse, no spawn."""
        from harnesses import HeadlessPlan as HP

        class _JsonOnly2:
            id = 'json-only-2'
            display_name = 'JSON Only 2'
            caps = HarnessCaps(headless_json=True, headless_chat=False)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return HP(argv=['echo', prompt], timeout=5)

            def parse_headless_output(self, stdout):
                return HeadlessResult(text=stdout.strip(), tokens=0)

        register_adapter(_JsonOnly2())
        settings = Settings(
            harness_default='json-only-2',
            claude_binary='/nonexistent/claude-chat-refuse-test',
        )
        settings.harnesses = {}
        with patch('subprocess.run') as mock_run:
            result = chat_turn(
                settings, 'resume me', session_id='old', project_path='')
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        assert 'headless_chat' in result.error

    def test_plan_none_falls_back_to_claude(self, _registry_snapshot):
        """chat_turn: implements but plan None → claude fallback."""
        class _NoneChat:
            id = 'none-chat'
            display_name = 'None Chat'
            caps = HarnessCaps(headless_json=True, headless_chat=True)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return None

            def parse_headless_output(self, stdout):
                return HeadlessResult(text='nope')

        register_adapter(_NoneChat())
        settings = Settings(harness_default='none-chat')
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json(
            'chat-via-claude', session_id='c1')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = chat_turn(settings, 'hi', project_path='')
        assert result.text == 'chat-via-claude'
        argv = mock_run.call_args[0][0]
        assert '-p' in argv

    def test_session_id_round_trip_parse(self):
        adapter = ClaudeAdapter()
        raw = _make_claude_json('pong', session_id='rt-99')
        parsed = adapter.parse_headless_output(raw)
        assert parsed.session_id == 'rt-99'
        # Resume plan uses that id
        plan = adapter.headless_plan(
            'next', Settings(), '', session_id=parsed.session_id, model='haiku')
        assert plan.argv[plan.argv.index('--resume') + 1] == 'rt-99'


# ---------------------------------------------------------------------------
# register_adapter headless defaults + instance override detection
# ---------------------------------------------------------------------------

class TestRegisterAdapterHeadlessDefaults:
    def test_custom_without_methods_gets_defaults(self, _registry_snapshot):
        """register_adapter binds default stubs so no AttributeError."""
        class _Bare:
            id = 'bare-custom'
            display_name = 'Bare'
            caps = HarnessCaps(headless_json=True)

        adapter = _Bare()
        assert not hasattr(adapter, 'headless_plan') or not callable(
            getattr(type(adapter), 'headless_plan', None))
        register_adapter(adapter)
        # Callable after registration
        assert callable(adapter.headless_plan)
        assert callable(adapter.parse_headless_output)
        # Default stubs do not count as implemented → resolve falls back
        assert adapter_implements_headless(adapter) is False
        assert adapter.headless_plan('x', Settings(), '') is None
        parsed = adapter.parse_headless_output('{}')
        assert parsed.text is None
        assert parsed.error

    def test_instance_override_counts_as_implemented(self, _registry_snapshot):
        """Real headless_plan on the instance is detected (not only class)."""
        import types
        from harnesses import HeadlessPlan as HP

        class _BaseStub:
            id = 'inst-override'
            display_name = 'Inst Override'
            caps = HarnessCaps(headless_json=True)

        adapter = _BaseStub()
        register_adapter(adapter)
        assert adapter_implements_headless(adapter) is False

        def _real_plan(self, prompt, settings, project_path, *,
                       session_id=None, model=None):
            return HP(argv=['echo', prompt], timeout=5)

        adapter.headless_plan = types.MethodType(_real_plan, adapter)
        assert adapter_implements_headless(adapter) is True

        settings = Settings(harness_default='inst-override')
        resolved, hid, reason = resolve_headless_adapter(settings, '')
        assert hid == 'inst-override'
        assert resolved is adapter
        assert reason is None


class TestFallbackLogOncePerScan:
    def test_run_ai_checks_logs_fallback_once(
            self, tmp_path, caplog, _registry_snapshot):
        """Three AI checks → one resolve-fallback log line, not three.

        Uses a stub harness (not a real W5 implementer) so resolve still
        falls back to claude.
        """
        import logging
        from harnesses import _default_headless_plan, _default_parse_headless_output
        from paa_haiku import run_ai_checks

        class _StubHL:
            id = 'stub-for-logonce'
            display_name = 'Stub LogOnce'
            caps = HarnessCaps(headless_json=True)
            headless_plan = _default_headless_plan
            parse_headless_output = _default_parse_headless_output

        register_adapter(_StubHL())
        proj = tmp_path / 'p'
        proj.mkdir()
        (proj / 'AGENTS.md').write_text('# x\n')
        (proj / 'requirements.txt').write_text('foo==1\n')
        settings = Settings(
            harness_default='stub-for-logonce',
            paa_allow_haiku=True,
            projects_dir=str(tmp_path),
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('{"issues": []}')
        with patch('subprocess.run', return_value=mock_result):
            with caplog.at_level(logging.INFO, logger='paa_haiku'):
                run_ai_checks('p', str(proj), settings)
        msgs = [
            r.message for r in caplog.records
            if 'paa scan harness fallback' in r.message
        ]
        assert len(msgs) == 1, msgs

    def test_run_ai_checks_logs_plan_none_fallback_once(
            self, tmp_path, caplog, _registry_snapshot):
        """Implementer headless_plan→None under run_ai_checks → one log.

        Resolve has no fallback_reason (adapter implements headless), so the
        only signal is plan-None → claude. Must not be silent (old bug:
        ``_log_scan_fallback=False`` suppressed plan-None for the whole
        three-check window).
        """
        import logging
        from paa_haiku import run_ai_checks

        class _NonePlanScan:
            id = 'none-plan-scan'
            display_name = 'None Plan Scan'
            caps = HarnessCaps(headless_json=True, headless_chat=False)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return None

            def parse_headless_output(self, stdout):
                return HeadlessResult(text=stdout, tokens=0)

        register_adapter(_NonePlanScan())
        # Sanity: resolve is clean; plan-None is the only fallback path.
        resolved, hid, resolve_reason = resolve_headless_adapter(
            Settings(harness_default='none-plan-scan'), '')
        assert resolve_reason is None
        assert resolved is not None
        assert getattr(resolved, 'id', None) == 'none-plan-scan'

        proj = tmp_path / 'p'
        proj.mkdir()
        (proj / 'AGENTS.md').write_text('# x\n')
        (proj / 'requirements.txt').write_text('foo==1\n')
        settings = Settings(
            harness_default='none-plan-scan',
            paa_allow_haiku=True,
            projects_dir=str(tmp_path),
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _make_claude_json('{"issues": []}')
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            with caplog.at_level(logging.INFO, logger='paa_haiku'):
                items, tokens = run_ai_checks('p', str(proj), settings)
        msgs = [
            r.message for r in caplog.records
            if 'paa scan harness fallback' in r.message
        ]
        assert len(msgs) == 1, msgs
        assert 'none-plan-scan' in msgs[0]
        assert 'headless_plan returned None' in msgs[0]
        # Still fell back to claude and ran (not silent empty failure).
        assert mock_run.called
        argv = mock_run.call_args[0][0]
        assert '-p' in argv or (len(argv) > 1 and argv[1] == '-p')


# ---------------------------------------------------------------------------
# Binary resolvable helper
# ---------------------------------------------------------------------------

def test_claude_binary_resolvable_missing():
    s = Settings(claude_binary='/no/such/claude-binary-paa-test')
    s.harnesses = {}
    assert claude_binary_resolvable(s) is False


def test_claude_binary_resolvable_on_path():
    # 'true' is always on PATH; reuse the helper shape with a fake settings
    s = Settings(claude_binary='true')
    s.harnesses = {}
    assert claude_binary_resolvable(s) is True


# ---------------------------------------------------------------------------
# Telegram chat turn: tool policy, timeout, cancel, visible fallback, budget
# ---------------------------------------------------------------------------

_FORBIDDEN_APPROVAL = ('--always-approve', 'bypassPermissions', 'dontAsk')


def _argv_has_forbidden(argv):
    blob = ' '.join(str(a) for a in argv)
    return [flag for flag in _FORBIDDEN_APPROVAL if flag in argv or flag in blob]


def _ok_run(stdout):
    return MagicMock(returncode=0, stdout=stdout)


class TestChatToolPolicy:
    def test_legacy_scan_plans_stay_30_and_unflagged(self):
        settings = Settings()
        grok = GrokAdapter().headless_plan('scan me', settings, '')
        claude = ClaudeAdapter().headless_plan(
            'scan prompt', settings, None, model='haiku')
        opencode = OpencodeAdapter().headless_plan('scan me', settings, '')
        kimi = KimiAdapter().headless_plan('scan me', settings, '')
        for plan in (grok, claude, opencode, kimi):
            assert plan.timeout == 30
            assert _argv_has_forbidden(plan.argv) == []
            assert '--permission-mode' not in plan.argv

    def test_grok_plan_and_armed_flags(self):
        adapter = GrokAdapter()
        settings = Settings()
        plan = adapter.headless_plan('p', settings, '', tool_policy='plan')
        assert plan.argv[plan.argv.index('--permission-mode') + 1] == 'plan'
        denials = [plan.argv[i + 1] for i, a in enumerate(plan.argv)
                   if a == '--deny']
        assert denials == ['Edit', 'Write', 'Bash', 'MCPTool']
        armed = adapter.headless_plan('p', settings, '', tool_policy='armed')
        assert armed.argv[armed.argv.index('--permission-mode') + 1] == 'acceptEdits'
        assert _argv_has_forbidden(plan.argv) == []
        assert _argv_has_forbidden(armed.argv) == []
        # Legacy shape is still there, and neither legacy nor armed deny.
        assert plan.argv[plan.argv.index('-p') + 1] == 'p'
        assert plan.argv[plan.argv.index('--output-format') + 1] == 'json'
        legacy = adapter.headless_plan('p', settings, '', tool_policy='legacy')
        assert '--deny' not in legacy.argv
        assert '--deny' not in armed.argv

    def test_chat_timeout_overrides_only_the_chat_plan(self):
        captured = {}

        def fake_run(plan, parse_fn, *, settings=None):
            captured['timeout'] = plan.timeout
            captured['argv'] = list(plan.argv)
            return HeadlessResult(text='ok', tokens=1, session_id='s')

        scan_captured = {}

        def fake_scan_run(plan, parse_fn, *, settings=None):
            scan_captured['timeout'] = plan.timeout
            return HeadlessResult(text='scan', tokens=0)

        with patch('paa_headless.run_headless', side_effect=fake_run):
            result = chat_turn(Settings(), 'hello', timeout=600)
        assert result.text == 'ok'
        assert captured['timeout'] == 600
        assert _argv_has_forbidden(captured['argv']) == []

        with patch('paa_headless.run_headless', side_effect=fake_run):
            chat_turn(Settings(), 'hello')
        assert captured['timeout'] == 30

        with patch('paa_headless.run_headless', side_effect=fake_scan_run):
            text, _tokens = _run_scan_model('scan', Settings(), project_path='')
        assert text == 'scan'
        assert scan_captured['timeout'] == 30

    def test_grok_chat_turn_passes_plan_flag(self):
        settings = Settings(harness_default='grok')
        mock = _ok_run(json.dumps({
            'text': 'no file', 'sessionId': 'sid',
            'usage': {'input_tokens': 1, 'output_tokens': 2},
        }))
        with patch('subprocess.run', return_value=mock) as mock_run:
            result = chat_turn(
                settings, 'do not write', tool_policy='plan', timeout=600)
        assert result.text == 'no file'
        assert result.error is None
        argv = mock_run.call_args[0][0]
        assert argv[argv.index('--permission-mode') + 1] == 'plan'
        denials = [argv[i + 1] for i, a in enumerate(argv) if a == '--deny']
        assert denials == ['Edit', 'Write', 'Bash', 'MCPTool']
        assert _argv_has_forbidden(argv) == []
        assert mock_run.call_args.kwargs['timeout'] == 600

    def test_opencode_and_kimi_non_legacy_do_not_build_argv(self):
        for hid in ('opencode', 'kimi'):
            adapter = ADAPTERS[hid]
            calls = []
            orig = adapter.headless_plan

            def wrapped(prompt, settings, project_path, *, session_id=None,
                        model=None, tool_policy='legacy', timeout=None,
                        _orig=orig, _calls=calls):
                _calls.append(tool_policy)
                return _orig(
                    prompt, settings, project_path,
                    session_id=session_id, model=model,
                    tool_policy=tool_policy, timeout=timeout,
                )

            settings = Settings(harness_default=hid)
            with patch.object(adapter, 'headless_plan', wrapped), \
                 patch('subprocess.run') as mock_run:
                result = chat_turn(settings, 'edit it', tool_policy='plan')
            mock_run.assert_not_called()
            assert calls == []
            assert result.text is None
            assert result.error
            assert 'unconstrained' in result.error
            assert hid in result.error
            # Direct plan also refuses to build argv.
            assert orig('p', Settings(), '', tool_policy='armed') is None

    def test_claude_maps_when_help_shows_spelling(self):
        from harnesses import permission_modes_in_help, read_claude_help
        settings = Settings()
        binary = settings.resolved_claude_binary
        help_text = read_claude_help(binary)
        modes = permission_modes_in_help(help_text)
        adapter = ClaudeAdapter()
        if 'plan' in modes and (
                'acceptEdits' in modes or 'accept-edits' in modes):
            plan = adapter.headless_plan(
                'p', settings, '', model='haiku', tool_policy='plan')
            armed = adapter.headless_plan(
                'p', settings, '', model='haiku', tool_policy='armed')
            assert plan.argv[plan.argv.index('--permission-mode') + 1] == 'plan'
            spelling = armed.argv[armed.argv.index('--permission-mode') + 1]
            assert spelling in modes
            assert _argv_has_forbidden(plan.argv) == []
            assert _argv_has_forbidden(armed.argv) == []
        else:
            import pytest
            with pytest.raises(Exception):
                adapter.headless_plan(
                    'p', settings, '', model='haiku', tool_policy='plan')

    def test_claude_missing_flag_is_an_error_and_does_not_spawn(self):
        from harnesses import HeadlessPolicyError
        settings = Settings()
        bare_help = 'usage: claude -p prompt\n'
        with patch('harnesses.read_claude_help', return_value=bare_help), \
             patch('subprocess.run') as mock_run:
            result = chat_turn(
                settings, 'write a file', tool_policy='plan')
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        assert 'missing flag' in result.error
        assert '--permission-mode' in result.error
        assert 'plan' in result.error
        with patch('harnesses.read_claude_help', return_value=bare_help):
            try:
                ClaudeAdapter().headless_plan(
                    'p', settings, '', model='haiku', tool_policy='armed')
            except HeadlessPolicyError as exc:
                assert 'acceptEdits' in str(exc)
                assert '--permission-mode' in str(exc)
            else:
                raise AssertionError('armed policy built argv without the flag')

    def test_cancel_kills_real_child(self, tmp_path):
        plan = HeadlessPlan(
            argv=['sleep', '30'], cwd=str(tmp_path), timeout=20)
        handle = HeadlessHandle()

        def _kill():
            for _ in range(50):
                if handle._proc is not None:
                    handle.kill()
                    return
                threading.Event().wait(0.02)
            handle.kill()

        threading.Thread(target=_kill, daemon=True).start()
        result = run_headless_cancellable(
            plan, lambda s: HeadlessResult(text=s or 'x'), handle=handle)
        assert result.text is None
        assert result.error

    def test_fallback_visible_and_error_empty_when_text_present(
            self, _registry_snapshot):
        class _NoneChat:
            id = 'none-chat-fb'
            display_name = 'None Chat FB'
            caps = HarnessCaps(headless_json=True, headless_chat=True)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                return None

            def parse_headless_output(self, stdout):
                return HeadlessResult(text='nope')

        register_adapter(_NoneChat())
        settings = Settings(harness_default='none-chat-fb')
        mock = _ok_run(_make_claude_json('via claude', session_id='c-fb'))
        with patch('subprocess.run', return_value=mock):
            result = chat_turn(settings, 'hi', project_path='')
        assert result.text == 'via claude'
        assert result.error is None
        assert result.fallback_reason
        assert 'claude' in result.fallback_reason

    def test_chat_turn_does_not_touch_budget(self):
        settings = Settings(paa_budget_used=42, paa_budget_tokens=100)
        mock = _ok_run(_make_claude_json('hi', input_tokens=9, output_tokens=8))
        with patch('subprocess.run', return_value=mock):
            result = chat_turn(settings, 'hello')
        assert result.text == 'hi'
        assert result.tokens == 17
        assert settings.paa_budget_used == 42


# ---------------------------------------------------------------------------
# Gate tool policy ('gate'): bypassPermissions + PAA_APPROVAL_SOCK, grok only
# ---------------------------------------------------------------------------

class TestGateToolPolicy:
    SOCK = '/tmp/paa-test-approval.sock'
    TOKEN = 'tok-test-123'

    def test_grok_gate_argv_and_env(self):
        settings = Settings()
        plan = GrokAdapter().headless_plan(
            'p', settings, '', tool_policy='gate', approval_sock=self.SOCK,
            approval_token=self.TOKEN)
        assert plan.argv[plan.argv.index('--permission-mode') + 1] == \
            'bypassPermissions'
        # Built-in mutators stay blocked; the MCP server is the mutation
        # channel now, so MCPTool is EXPLICITLY not denied.
        denials = [plan.argv[i + 1] for i, a in enumerate(plan.argv)
                   if a == '--deny']
        assert denials == ['Edit', 'Write', 'Bash']
        assert 'MCPTool' not in denials
        # Gate keeps the chat shape; only the permission posture changes.
        assert plan.argv[plan.argv.index('-p') + 1] == 'p'
        assert plan.argv[plan.argv.index('--output-format') + 1] == 'json'
        # Env REPLACES the child environment, so it must merge os.environ.
        assert plan.env['PAA_APPROVAL_SOCK'] == self.SOCK
        assert plan.env['PAA_APPROVAL_TOKEN'] == self.TOKEN
        for key in ('PATH', 'HOME'):
            assert plan.env[key] == os.environ[key]
        assert len(plan.env) > 2
        assert plan.env is not os.environ

    def test_grok_gate_resume_and_timeout_kept(self):
        settings = Settings()
        plan = GrokAdapter().headless_plan(
            'p', settings, '', session_id='sid', tool_policy='gate',
            approval_sock=self.SOCK, approval_token=self.TOKEN, timeout=600)
        assert plan.argv[plan.argv.index('-r') + 1] == 'sid'
        assert plan.timeout == 600
        assert plan.env['PAA_APPROVAL_SOCK'] == self.SOCK
        assert plan.env['PAA_APPROVAL_TOKEN'] == self.TOKEN

    @pytest.mark.parametrize('channel', [
        {},  # neither half
        {'approval_sock': SOCK},  # token missing
        {'approval_token': TOKEN},  # sock missing
        {'approval_sock': SOCK, 'approval_token': ''},  # empty token
    ])
    def test_grok_gate_half_channel_raises_and_never_spawns(self, channel):
        from harnesses import HeadlessPolicyError
        with pytest.raises(HeadlessPolicyError):
            GrokAdapter().headless_plan('p', Settings(), '',
                                        tool_policy='gate', **channel)
        settings = Settings(harness_default='grok')
        with patch('subprocess.run') as mock_run:
            result = chat_turn(settings, 'edit it', tool_policy='gate',
                               **channel)
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        assert 'gate' in result.error
        assert 'approval' in result.error

    def test_grok_gate_without_sock_raises_and_never_spawns(self):
        from harnesses import HeadlessPolicyError
        with pytest.raises(HeadlessPolicyError):
            GrokAdapter().headless_plan('p', Settings(), '',
                                        tool_policy='gate')
        settings = Settings(harness_default='grok')
        with patch('subprocess.run') as mock_run:
            result = chat_turn(settings, 'edit it', tool_policy='gate')
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        assert 'gate' in result.error
        assert 'approval' in result.error

    def test_grok_chat_turn_gate_threads_sock_and_token(self):
        settings = Settings(harness_default='grok')
        mock = _ok_run(json.dumps({
            'text': 'edited', 'sessionId': 'sid',
            'usage': {'input_tokens': 1, 'output_tokens': 2},
        }))
        with patch('subprocess.run', return_value=mock) as mock_run:
            result = chat_turn(
                settings, 'edit the file', tool_policy='gate',
                approval_sock=self.SOCK, approval_token=self.TOKEN,
                timeout=600)
        assert result.text == 'edited'
        assert result.error is None
        argv = mock_run.call_args[0][0]
        assert argv[argv.index('--permission-mode') + 1] == 'bypassPermissions'
        denials = [argv[i + 1] for i, a in enumerate(argv) if a == '--deny']
        assert denials == ['Edit', 'Write', 'Bash']
        assert 'MCPTool' not in denials
        env = mock_run.call_args.kwargs['env']
        assert env['PAA_APPROVAL_SOCK'] == self.SOCK
        assert env['PAA_APPROVAL_TOKEN'] == self.TOKEN
        assert env['PATH'] == os.environ['PATH']
        assert mock_run.call_args.kwargs['timeout'] == 600

    @pytest.mark.parametrize('hid', ['claude', 'opencode', 'kimi'])
    def test_gate_on_unsupported_adapters_errors_without_spawn(self, hid):
        settings = Settings(harness_default=hid)
        with patch('subprocess.run') as mock_run:
            result = chat_turn(
                settings, 'edit it', tool_policy='gate',
                approval_sock=self.SOCK)
        mock_run.assert_not_called()
        assert result.text is None
        assert result.error
        if hid in ('opencode', 'kimi'):
            assert 'unconstrained' in result.error
        else:
            assert 'unknown tool_policy' in result.error

    def test_gate_plan_goldens_unchanged(self):
        """legacy/plan/armed argv are untouched by the gate branch; every
        policy gets compat-MCP isolation env (2026-10-01)."""
        adapter = GrokAdapter()
        settings = Settings()
        legacy = adapter.headless_plan('p', settings, '')
        plan = adapter.headless_plan('p', settings, '', tool_policy='plan')
        armed = adapter.headless_plan('p', settings, '', tool_policy='armed')
        for p in (legacy, plan, armed):
            assert p.env['GROK_CLAUDE_MCPS_ENABLED'] == '0'
            assert p.env['GROK_CURSOR_MCPS_ENABLED'] == '0'
            assert 'PAA_APPROVAL_SOCK' not in p.env
            assert 'PAA_APPROVAL_TOKEN' not in p.env
        assert '--permission-mode' not in legacy.argv
        assert '--deny' not in legacy.argv
        assert '--disallowed-tools' not in legacy.argv
        assert plan.argv[plan.argv.index('--permission-mode') + 1] == 'plan'
        assert [plan.argv[i + 1] for i, a in enumerate(plan.argv)
                if a == '--deny'] == ['Edit', 'Write', 'Bash', 'MCPTool']
        assert '--disallowed-tools' not in plan.argv
        assert armed.argv[armed.argv.index('--permission-mode') + 1] == \
            'acceptEdits'
        assert '--disallowed-tools' not in armed.argv

    def test_gate_argv_disallowed_tools_belt_and_suspenders(self):
        """Probe-verified 2026-10-01: run_terminal_cmd is the real shell
        id; the deny rules stay as the first layer."""
        plan = GrokAdapter().headless_plan(
            'p', Settings(), '', tool_policy='gate',
            approval_sock=self.SOCK, approval_token=self.TOKEN)
        index = plan.argv.index('--disallowed-tools')
        value = plan.argv[index + 1]
        assert 'run_terminal_cmd' in value
        assert 'write' in value.split(',')
        assert 'search_replace' in value.split(',')
        # deny rules still present, MCPTool still not denied
        denials = [plan.argv[i + 1] for i, a in enumerate(plan.argv)
                   if a == '--deny']
        assert denials == ['Edit', 'Write', 'Bash']


# ---------------------------------------------------------------------------
# reasoning_effort (item 4, 2026-10-01): reviewer effort threaded to argv
# ---------------------------------------------------------------------------

class TestReasoningEffort:
    def test_grok_appends_flag_for_any_policy(self):
        adapter = GrokAdapter()
        settings = Settings()
        plan = adapter.headless_plan('p', settings, '',
                                     reasoning_effort='low')
        assert plan.argv[plan.argv.index('--reasoning-effort') + 1] == 'low'
        gate = adapter.headless_plan('p', settings, '', tool_policy='gate',
                                     approval_sock='/tmp/s.sock',
                                     approval_token='t',
                                     reasoning_effort='high')
        assert gate.argv[gate.argv.index('--reasoning-effort') + 1] == 'high'
        legacy = adapter.headless_plan('p', settings, '',
                                       reasoning_effort='minimal')
        assert legacy.argv[legacy.argv.index('--reasoning-effort') + 1] == \
            'minimal'

    def test_none_and_unset_emit_no_flag(self):
        adapter = GrokAdapter()
        settings = Settings()
        for effort in (None, 'none'):
            plan = adapter.headless_plan('p', settings, '',
                                         reasoning_effort=effort)
            assert '--reasoning-effort' not in plan.argv

    def test_chat_turn_threads_effort_into_argv(self):
        settings = Settings(harness_default='grok')
        mock = _ok_run(json.dumps({
            'text': 'ok', 'sessionId': 'sid',
            'usage': {'input_tokens': 1, 'output_tokens': 2},
        }))
        with patch('subprocess.run', return_value=mock) as mock_run:
            result = chat_turn(settings, 'review this', tool_policy='plan',
                               reasoning_effort='medium', timeout=600)
        assert result.text == 'ok'
        argv = mock_run.call_args[0][0]
        assert argv[argv.index('--reasoning-effort') + 1] == 'medium'

    def test_other_adapters_ignore_effort_silently(self):
        """Adapters without the kwarg never see it; argv is unchanged.
        (opencode/kimi refuse non-legacy policies outright — covered by
        the unconstrained test — so claude is the forwarding-proof case.)"""
        settings = Settings(harness_default='claude')
        mock = _ok_run(_make_claude_json('ok'))
        with patch('subprocess.run', return_value=mock) as mock_run:
            result = chat_turn(settings, 'review this', tool_policy='plan',
                               reasoning_effort='medium', timeout=600)
        assert result.text == 'ok'
        argv = mock_run.call_args[0][0]
        assert '--reasoning-effort' not in argv
        # Mechanism proof: an adapter whose plan signature lacks the
        # kwarg never receives it.
        from paa_headless import _invoke_headless_plan
        seen = {}

        class _NoEffort:
            id = 'no-effort'
            caps = HarnessCaps(headless_json=True)

            def headless_plan(self, prompt, settings, project_path, *,
                              session_id=None, model=None):
                seen['called'] = True
                return HeadlessPlan(argv=['x'], timeout=5)

        plan = _invoke_headless_plan(
            _NoEffort(), 'p', Settings(), '', reasoning_effort='medium')
        assert plan is not None and seen['called']

    def test_unconstrained_adapters_still_refuse_with_effort_set(self):
        settings = Settings(harness_default='opencode')
        with patch('subprocess.run') as mock_run:
            result = chat_turn(settings, 'edit it', tool_policy='plan',
                               reasoning_effort='low')
        mock_run.assert_not_called()
        assert 'unconstrained' in result.error


# ---------------------------------------------------------------------------
# sync_paa_persona (item 5, 2026-10-01): deployed persona docs stay current
# ---------------------------------------------------------------------------

class TestSyncPaaPersona:
    def _repo(self, tmp_path, files=None):
        repo = tmp_path / 'paa'
        repo.mkdir(parents=True)
        defaults = {
            'AGENTS.md': '# persona\nShell Access section\n',
            'AGENTS-SUPPLEMENT.md': '# supplement\n',
        }
        for name, text in (files or defaults).items():
            (repo / name).write_text(text)
        return repo

    def test_creates_deployed_files_and_is_idempotent(self, tmp_path):
        from paa_headless import sync_paa_persona
        repo = self._repo(tmp_path)
        cwd = tmp_path / 'paa-cwd'
        results = sync_paa_persona(str(cwd), repo_dir=str(repo))
        assert ('AGENTS.md', True) in results
        assert (os.path.join('.system', 'AGENTS-SUPPLEMENT.md'), True) in \
            results
        assert (cwd / 'AGENTS.md').read_text() == \
            '# persona\nShell Access section\n'
        assert (cwd / '.system' / 'AGENTS-SUPPLEMENT.md').read_text() == \
            '# supplement\n'
        again = sync_paa_persona(str(cwd), repo_dir=str(repo))
        assert all(changed is False for _name, changed in again), again

    def test_refreshes_stale_copy(self, tmp_path):
        from paa_headless import sync_paa_persona
        repo = self._repo(tmp_path)
        cwd = tmp_path / 'paa-cwd'
        cwd.mkdir()
        (cwd / 'AGENTS.md').write_text('# ancient\n')
        results = sync_paa_persona(str(cwd), repo_dir=str(repo))
        assert ('AGENTS.md', True) in results
        assert 'Shell Access section' in (cwd / 'AGENTS.md').read_text()

    def test_missing_repo_source_skips_without_erasing(self, tmp_path):
        from paa_headless import sync_paa_persona
        repo = self._repo(tmp_path, files={'AGENTS.md': '# only persona\n'})
        cwd = tmp_path / 'paa-cwd'
        cwd.mkdir()
        (cwd / '.system').mkdir()
        (cwd / '.system' / 'AGENTS-SUPPLEMENT.md').write_text('# working\n')
        results = sync_paa_persona(str(cwd), repo_dir=str(repo))
        names = [name for name, _changed in results]
        assert os.path.join('.system', 'AGENTS-SUPPLEMENT.md') not in names
        assert (cwd / '.system' / 'AGENTS-SUPPLEMENT.md').read_text() == \
            '# working\n'


def test_sync_paa_persona_removes_stale_claude_md(tmp_path):
    """Review F2 2026-10-01: a deployed CLAUDE.md is a stale artifact that
    grok's compat.claude.agents scan would load beside AGENTS.md."""
    from paa_headless import sync_paa_persona
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'AGENTS.md').write_text('persona v2')
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / 'CLAUDE.md').write_text('stale claude-era persona')
    results = sync_paa_persona(str(cwd), repo_dir=str(repo))
    assert not (cwd / 'CLAUDE.md').exists()
    assert ('CLAUDE.md', True) in results
    # Idempotent: second run reports no change.
    again = sync_paa_persona(str(cwd), repo_dir=str(repo))
    assert ('CLAUDE.md', True) not in again
    # Never creates one.
    assert not (cwd / 'CLAUDE.md').exists()
