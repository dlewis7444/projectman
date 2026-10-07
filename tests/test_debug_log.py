"""debug_log helper + session-launch debug lines (no GTK)."""

from settings import Settings
from debug_log import debug_enabled, debug_log, format_argv_for_debug


def test_debug_enabled_respects_flag():
    assert debug_enabled(None) is False
    assert debug_enabled(Settings(debug_logging=False)) is False
    assert debug_enabled(Settings(debug_logging=True)) is True


def test_debug_log_silent_when_off(capsys):
    debug_log(Settings(debug_logging=False), 'should not print')
    debug_log(None, 'also silent')
    assert capsys.readouterr().out == ''


def test_debug_log_prints_dbg_prefix(capsys):
    debug_log(Settings(debug_logging=True), 'hello world')
    out = capsys.readouterr().out
    assert out == '[DBG] hello world\n'


def test_format_argv_truncates_long_args():
    long = 'x' * 200
    s = format_argv_for_debug(['grok', '-p', long], max_arg=20, max_total=200)
    assert '…' in s
    assert len(s) <= 200


def test_run_headless_logs_session_launch_when_debug(capsys, monkeypatch):
    from harnesses import HeadlessPlan, HeadlessResult
    from paa_headless import run_headless
    import paa_headless as ph

    plan = HeadlessPlan(
        argv=['grok', '-p', 'hi', '--output-format', 'json'],
        cwd='/tmp',
        timeout=5,
    )
    settings = Settings(debug_logging=True)

    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = '{"text":"ok"}'
            stderr = ''
        return R()

    monkeypatch.setattr(ph.subprocess, 'run', fake_run)

    def parse(_stdout):
        return HeadlessResult(text='ok', tokens=1)

    run_headless(plan, parse, settings=settings)
    out = capsys.readouterr().out
    assert '[DBG] session launch kind=headless' in out
    assert 'grok' in out
    assert '-p' in out


def test_run_headless_silent_when_debug_off(capsys, monkeypatch):
    from harnesses import HeadlessPlan, HeadlessResult
    from paa_headless import run_headless
    import paa_headless as ph

    plan = HeadlessPlan(argv=['claude', '-p', 'x'], cwd='/tmp', timeout=5)
    settings = Settings(debug_logging=False)

    def fake_run(*a, **k):
        class R:
            returncode = 0
            stdout = '{}'
            stderr = ''
        return R()

    monkeypatch.setattr(ph.subprocess, 'run', fake_run)
    run_headless(
        plan, lambda s: HeadlessResult(text='t'), settings=settings)
    assert '[DBG]' not in capsys.readouterr().out


def test_run_scan_model_logs_harness_when_debug(capsys, monkeypatch, tmp_path):
    """_run_scan_model emits paa scan harness=… under debug_logging."""
    from paa_haiku import _run_scan_model
    import paa_headless as phl
    from harnesses import HeadlessPlan, HeadlessResult, get_adapter

    settings = Settings(
        debug_logging=True,
        harness_default='claude',
        projects_dir=str(tmp_path),
        claude_binary='claude',
    )
    adapter = get_adapter('claude', settings)
    plan = HeadlessPlan(
        argv=['claude', '-p', '--model', 'haiku', '--output-format', 'json', 'q'],
        cwd=str(tmp_path / '.project-admin-agent'),
        timeout=30,
    )
    monkeypatch.setattr(
        phl, 'resolve_headless_adapter',
        lambda s, p: (adapter, 'claude', None),
    )
    monkeypatch.setattr(
        phl, 'plan_with_none_fallback',
        lambda *a, **k: (adapter, plan, None),
    )
    monkeypatch.setattr(
        phl, 'run_headless',
        lambda plan, parse_fn, *, settings=None: HeadlessResult(
            text='{"issues":[]}', tokens=3),
    )

    _run_scan_model('prompt', settings, project_path=str(tmp_path))
    out = capsys.readouterr().out
    assert 'paa scan harness=claude' in out
