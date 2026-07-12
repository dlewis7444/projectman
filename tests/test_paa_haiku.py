import os
import json
import subprocess
from unittest.mock import patch, MagicMock
from settings import Settings
from paa_haiku import (
    _run_scan_model, _run_haiku, _parse_haiku_response,
    check_semantic_staleness, check_dependency_versions,
    check_project_health, run_ai_checks,
)


def _make_claude_json(result_text, input_tokens=50, output_tokens=100):
    """Helper: build a mock claude --output-format json response."""
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False,
        'result': result_text,
        'usage': {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cache_read_input_tokens': 0,
            'cache_creation_input_tokens': 0,
        },
        'total_cost_usd': 0.001,
    })


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


def test_run_haiku_success():
    settings = Settings()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json('hello world')
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        text, tokens = _run_haiku('test prompt', settings)
    assert text == 'hello world'
    assert tokens == 150  # input + output
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert '--output-format' in args[0][0] or '--output-format' in str(args)
    # Back-compat path (project_path=None): native inherit, bare tier alias
    assert args.kwargs.get('env') is None
    assert '--model' in args[0][0]
    model_idx = args[0][0].index('--model')
    assert args[0][0][model_idx + 1] == 'haiku'


def test_run_haiku_timeout():
    settings = Settings()
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('claude', 30)):
        text, tokens = _run_haiku('test', settings)
    assert text is None
    assert tokens == 0


def test_run_haiku_not_found():
    settings = Settings()
    with patch('subprocess.run', side_effect=FileNotFoundError):
        text, tokens = _run_haiku('test', settings)
    assert text is None
    assert tokens == 0


def test_run_haiku_nonzero_exit():
    settings = Settings()
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ''
    with patch('subprocess.run', return_value=mock_result):
        text, tokens = _run_haiku('test', settings)
    assert text is None
    assert tokens == 0


def test_run_haiku_bad_json():
    settings = Settings()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = 'not json at all'
    with patch('subprocess.run', return_value=mock_result):
        text, tokens = _run_haiku('test', settings)
    assert text is None
    assert tokens == 0


def test_run_scan_model_custom_provider_injects_env_and_resolves_tier():
    """Custom model_default → ANTHROPIC_BASE_URL + resolved tier model id."""
    settings = Settings(
        providers=_ollama_providers(),
        model_default='ollama',
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
        text, tokens = _run_scan_model('scan me', settings, project_path='/proj/foo')
    assert text == 'ok'
    assert tokens == 150
    args = mock_run.call_args
    argv = args[0][0]
    assert argv[argv.index('--model') + 1] == 'ministral-3'
    env = args.kwargs.get('env')
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'secret-key'
    assert env['ANTHROPIC_API_KEY'] == ''
    assert env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'ministral-3'


def test_run_scan_model_native_with_project_path_no_base_url():
    """Native model_default + project_path: inherit env, bare tier alias."""
    settings = Settings(paa_scan_model='sonnet')
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json('native')
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        text, _ = _run_scan_model('p', settings, project_path='/proj/foo')
    assert text == 'native'
    args = mock_run.call_args
    argv = args[0][0]
    assert argv[argv.index('--model') + 1] == 'sonnet'
    assert args.kwargs.get('env') is None


def test_run_scan_model_unusable_provider_falls_back_native():
    """Custom provider without base_url → native fallback (env None, bare tier)."""
    settings = Settings(
        providers={'bad': {'name': 'Bad', 'base_url': '', 'api_key': '',
                           'models': ['x']}},
        model_default='bad',
        paa_scan_model='haiku',
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json('fb')
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        text, _ = _run_scan_model('p', settings, project_path='/proj/foo')
    assert text == 'fb'
    args = mock_run.call_args
    argv = args[0][0]
    assert argv[argv.index('--model') + 1] == 'haiku'
    assert args.kwargs.get('env') is None


def test_check_semantic_staleness_passes_project_path_to_scan(tmp_path):
    """AI check functions must thread project_path into the scan runner."""
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text('# docs\n')
    settings = Settings(
        providers=_ollama_providers(models=['mini', 'big']),
        model_default='ollama',
        paa_scan_model='haiku',
        tier_models={'ollama': {'haiku': 'mini'}},
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json('{"issues": []}')
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        items, tokens = check_semantic_staleness('myproj', str(proj), settings)
    assert items == []
    assert tokens == 150
    env = mock_run.call_args.kwargs.get('env')
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'
    argv = mock_run.call_args[0][0]
    assert argv[argv.index('--model') + 1] == 'mini'


def test_parse_haiku_response_valid():
    issues = _parse_haiku_response('{"issues": [{"summary": "bad thing", "evidence": "line 5"}]}')
    assert len(issues) == 1
    assert issues[0]['summary'] == 'bad thing'


def test_parse_haiku_response_empty():
    issues = _parse_haiku_response('{"issues": []}')
    assert issues == []


def test_parse_haiku_response_invalid_json():
    issues = _parse_haiku_response('not json')
    assert issues == []


def test_parse_haiku_response_missing_key():
    issues = _parse_haiku_response('{"results": []}')
    assert issues == []


def test_check_semantic_staleness_finds_issue(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text('# Old docs\nReferences `old_module.py`')
    (proj / 'main.py').write_text('print("hello")')

    response_json = '{"issues": [{"summary": "CLAUDE.md references old_module.py which does not exist", "evidence": "old_module.py"}]}'
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json(response_json, 100, 200)

    settings = Settings()
    with patch('subprocess.run', return_value=mock_result):
        items, tokens = check_semantic_staleness('myproj', str(proj), settings)
    assert len(items) == 1
    assert items[0].type == 'ai-semantic-staleness'
    assert tokens == 300


def test_check_semantic_staleness_no_claude_md(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    settings = Settings()
    items, tokens = check_semantic_staleness('myproj', str(proj), settings)
    assert items == []
    assert tokens == 0


def test_check_dependency_versions_no_manifest(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    settings = Settings()
    items, tokens = check_dependency_versions('myproj', str(proj), settings)
    assert items == []
    assert tokens == 0


def test_check_dependency_versions_finds_issue(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    (proj / 'requirements.txt').write_text('flask==1.0\nrequests==2.20.0\n')

    response_json = '{"issues": [{"summary": "flask 1.0 is severely outdated", "evidence": "flask==1.0"}]}'
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _make_claude_json(response_json, 80, 150)

    settings = Settings()
    with patch('subprocess.run', return_value=mock_result):
        items, tokens = check_dependency_versions('myproj', str(proj), settings)
    assert len(items) == 1
    assert items[0].type == 'ai-dependency-outdated'
    assert tokens == 230


def test_run_ai_checks_haiku_disabled(tmp_path):
    proj = tmp_path / 'myproj'
    proj.mkdir()
    settings = Settings(paa_allow_haiku=False)
    items, tokens = run_ai_checks('myproj', str(proj), settings)
    assert items == []
    assert tokens == 0
