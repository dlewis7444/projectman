import os
import json
import subprocess
from unittest.mock import patch, MagicMock
from settings import Settings
from paa_haiku import (
    _run_haiku, _parse_haiku_response,
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
