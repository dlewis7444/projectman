import os
import json
import subprocess

import ccr
from settings import Settings


class _FakeProc:
    """Stand-in for the Popen handle. poll() returns ``poll_value``."""

    def __init__(self, poll_value=None, pid=4242):
        self._poll_value = poll_value
        self.pid = pid
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return self._poll_value


def _settings(**kw):
    base = dict(
        providers={
            'ollama': {
                'name': 'Ollama',
                'base_url': 'http://host:11434/v1',
                'api_key': 'k',
                'models': {'qwen': {'name': 'Qwen'}},
            },
        },
        model_default='ollama/qwen',
        ccr_api_key='secret',
    )
    base.update(kw)
    return Settings(**base)


def _redirect_config(monkeypatch, tmp_path):
    cfg_dir = tmp_path / '.ccr'
    monkeypatch.setattr(ccr, 'CCR_CONFIG_DIR', str(cfg_dir))
    monkeypatch.setattr(ccr, 'CCR_CONFIG_PATH', str(cfg_dir / 'config.json'))
    return cfg_dir


def test_render_config_shape():
    cfg = ccr.render_config(_settings())
    assert cfg['HOST'] == '127.0.0.1'
    assert cfg['PORT'] == 3456
    assert cfg['APIKEY'] == 'secret'
    assert cfg['Providers'] == [{
        'name': 'ollama',
        'api_base_url': 'http://host:11434/v1',
        'api_key': 'k',
        'models': ['qwen'],
    }]
    assert cfg['Router']['default'] == 'ollama,qwen'
    assert cfg['Router']['longContext'] == 'ollama,qwen'


def test_render_config_includes_transformer():
    s = _settings()
    s.providers['ollama']['transformer'] = 'openrouter'
    cfg = ccr.render_config(s)
    assert cfg['Providers'][0]['transformer'] == {'use': ['openrouter']}


def test_router_target_prefers_global_default():
    assert ccr._router_target(_settings()) == 'ollama,qwen'


def test_router_target_falls_back_to_override():
    s = _settings(model_default='', model_overrides={'/p/a': 'ollama/qwen'})
    assert ccr._router_target(s) == 'ollama,qwen'


def test_router_target_falls_back_to_first_provider():
    s = _settings(model_default='', model_overrides={})
    assert ccr._router_target(s) == 'ollama,qwen'


def test_ensure_api_key_mints_when_blank():
    s = _settings(ccr_api_key='')
    key = ccr.ensure_api_key(s)
    assert key and s.ccr_api_key == key
    # idempotent — a second call keeps the same key
    assert ccr.ensure_api_key(s) == key


def test_write_config_atomic_and_hardened(monkeypatch, tmp_path):
    cfg_dir = _redirect_config(monkeypatch, tmp_path)
    assert ccr.write_config(_settings()) is True
    cfg_path = cfg_dir / 'config.json'
    assert cfg_path.exists()
    assert (os.stat(cfg_path).st_mode & 0o777) == 0o600
    assert (os.stat(cfg_dir).st_mode & 0o777) == 0o700
    # no temp files left behind
    assert {p.name for p in cfg_dir.iterdir()} == {'config.json'}
    assert json.loads(cfg_path.read_text())['APIKEY'] == 'secret'


def test_config_differs(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    s = _settings()
    # no file yet
    assert ccr.config_differs(s) is True
    ccr.write_config(s)
    assert ccr.config_differs(s) is False
    # a changed selection is detected
    s.model_default = 'ollama/other'
    s.providers['ollama']['models']['other'] = {'name': 'Other'}
    assert ccr.config_differs(s) is True


def test_available_false_for_bogus_binary():
    assert ccr.available(Settings(ccr_binary='/no/such/ccr-binary-xyz')) is False


# ---------------------------------------------------------------------------
# spawn_env — acceptance criteria 1-4
# ---------------------------------------------------------------------------

PROJECT_PATH = '/projects/myproj'


def _custom_settings(**kw):
    """Settings with a custom provider/model for PROJECT_PATH."""
    base = dict(
        providers={
            'ollama': {
                'name': 'Ollama',
                'base_url': 'http://host:11434/v1',
                'api_key': 'k',
                'models': {'qwen': {'name': 'Qwen'}},
            },
        },
        model_default='ollama/qwen',
        ccr_host='127.0.0.1',
        ccr_port=3456,
        ccr_api_key='secret',
    )
    base.update(kw)
    return Settings(**base)


def test_spawn_env_native_project_returns_none_none():
    """Criterion 4: native-model project → (None, None), probe never called."""
    probe_calls = []
    s = _custom_settings(model_default='')   # no custom model
    env, reason = ccr.spawn_env(s, PROJECT_PATH, probe=lambda _s: probe_calls.append(1) or True)
    assert env is None
    assert reason is None
    assert probe_calls == [], 'probe must not be called on native path'


def test_spawn_env_ccr_missing_returns_fallback(monkeypatch):
    """Criterion 1: ccr binary missing → (None, non-empty reason)."""
    monkeypatch.setattr(ccr, 'available', lambda _s: False)
    s = _custom_settings()
    env, reason = ccr.spawn_env(s, PROJECT_PATH)
    assert env is None
    assert reason and isinstance(reason, str)


def test_spawn_env_ccr_running_returns_env(monkeypatch):
    """Criterion 3: ccr running → env with all four vars at correct values."""
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    s = _custom_settings()
    env, reason = ccr.spawn_env(s, PROJECT_PATH, probe=lambda _s: True)
    assert reason is None
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == f'http://{s.ccr_host}:{s.ccr_port}'
    assert env['ANTHROPIC_AUTH_TOKEN'] == s.ccr_api_key
    assert env['ANTHROPIC_API_KEY'] == s.ccr_api_key
    # model must use comma form, e.g. 'ollama,qwen'
    assert env['ANTHROPIC_MODEL'] == 'ollama,qwen'


def test_spawn_env_ccr_installed_not_running_start_fails(monkeypatch):
    """Criterion 2: ccr installed, not running, never comes up → (None, reason).

    Small start_wait bounds the poll; no-op sleep_fn keeps the test instant.
    """
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    monkeypatch.setattr(ccr, 'start', lambda _s: False)
    s = _custom_settings()
    probe_calls = []
    def probe(_s):
        probe_calls.append(1)
        return False  # never comes up
    env, reason = ccr.spawn_env(s, PROJECT_PATH, probe=probe,
                                start_wait=0.5, sleep_fn=lambda _t: None)
    assert env is None
    assert reason and isinstance(reason, str)
    # count-based poll: 1 pre-start probe + max(1, int(0.5/0.25)) = 2 retries
    assert len(probe_calls) == 3


def test_spawn_env_ccr_installed_not_running_start_succeeds(monkeypatch):
    """Criterion 2 (success variant): ccr not running, start succeeds → env returned."""
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    monkeypatch.setattr(ccr, 'start', lambda _s: True)
    s = _custom_settings()
    # probe: first call False (not yet running), second call True (started)
    call_count = [0]
    def probe(_s):
        call_count[0] += 1
        # First probe: not running; after start(), True
        return call_count[0] > 1
    env, reason = ccr.spawn_env(s, PROJECT_PATH, probe=probe,
                                sleep_fn=lambda _t: None)
    assert reason is None
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == f'http://{s.ccr_host}:{s.ccr_port}'


def test_spawn_env_polls_through_ccr_cold_start(monkeypatch):
    """`ccr start` returns before its node server binds the port (~0.9s vs
    ~2.5s measured), so spawn_env must keep polling instead of falling back
    on the first closed-port probe after start()."""
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    monkeypatch.setattr(ccr, 'start', lambda _s: True)
    s = _custom_settings()
    results = iter([False, False, True])   # pre-start check, poll 1, poll 2
    sleeps = []
    env, reason = ccr.spawn_env(
        s, PROJECT_PATH,
        probe=lambda _s: next(results),
        sleep_fn=sleeps.append,
    )
    assert reason is None
    assert env is not None
    assert env['ANTHROPIC_BASE_URL'] == f'http://{s.ccr_host}:{s.ccr_port}'
    assert sleeps == [0.25, 0.25]


# ---------------------------------------------------------------------------
# Detached start / restart / reap — process-lifecycle (round 3)
#
# ccr v2.0.0 runs its server in the FOREGROUND in non-tty contexts: the CLI
# process IS the server and never daemonizes. subprocess.run(..., timeout) thus
# kills the server it just started when the timeout fires. start() must spawn
# detached (Popen, own session) and let the probe be the readiness arbiter.
# ---------------------------------------------------------------------------


def _reset_started():
    ccr._started_proc = None


def test_start_spawns_detached_and_records_handle(monkeypatch):
    """start() uses Popen (not run-with-timeout): correct argv + detach kwargs,
    handle stashed in _started_proc, returns True."""
    _reset_started()
    captured = {}
    fake = _FakeProc()

    def fake_popen(argv, **kwargs):
        captured['argv'] = argv
        captured['kwargs'] = kwargs
        return fake

    monkeypatch.setattr(ccr.subprocess, 'Popen', fake_popen)
    s = _custom_settings(ccr_binary='/usr/local/bin/ccr')

    assert ccr.start(s) is True
    assert captured['argv'] == ['/usr/local/bin/ccr', 'start']
    kw = captured['kwargs']
    # detached: own session so PM-exit signals don't reach it, no controlling tty
    assert kw['start_new_session'] is True
    # no pipes / no waiting — fully decoupled from PM's stdio
    assert kw['stdin'] == subprocess.DEVNULL
    assert kw['stdout'] == subprocess.DEVNULL
    assert kw['stderr'] == subprocess.DEVNULL
    assert ccr._started_proc is fake
    _reset_started()


def test_start_returns_false_when_binary_missing(monkeypatch):
    """FileNotFoundError/OSError from Popen → False, handle left cleared."""
    _reset_started()

    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(ccr.subprocess, 'Popen', boom)
    s = _custom_settings(ccr_binary='/no/such/ccr')
    assert ccr.start(s) is False
    assert ccr._started_proc is None


def test_start_does_not_use_run(monkeypatch):
    """Guard against regressing to subprocess.run (which kills the server)."""
    _reset_started()

    def fail_run(*a, **k):
        raise AssertionError('start() must not call subprocess.run')

    monkeypatch.setattr(ccr.subprocess, 'run', fail_run)
    monkeypatch.setattr(ccr.subprocess, 'Popen', lambda *a, **k: _FakeProc())
    ccr.start(_custom_settings())
    _reset_started()


def test_restart_stops_then_starts_detached(monkeypatch):
    """restart() = stop() via _run (prompt-exiting CLI) THEN detached start();
    ordering matters and it returns start()'s result."""
    _reset_started()
    order = []

    def fake_run(settings, *args):
        order.append(('run',) + args)
        return True

    def fake_start(settings):
        order.append(('start',))
        return True

    monkeypatch.setattr(ccr, '_run', fake_run)
    monkeypatch.setattr(ccr, 'start', fake_start)
    assert ccr.restart(_custom_settings()) is True
    assert order == [('run', 'stop'), ('start',)]
    _reset_started()


def test_restart_returns_start_result(monkeypatch):
    """restart() propagates start()'s False even if stop() succeeded."""
    _reset_started()
    monkeypatch.setattr(ccr, '_run', lambda s, *a: True)
    monkeypatch.setattr(ccr, 'start', lambda s: False)
    assert ccr.restart(_custom_settings()) is False
    _reset_started()


def test_reap_started_clears_finished_handle():
    """A handle whose process exited (poll() returns an int) is cleared."""
    ccr._started_proc = _FakeProc(poll_value=0)
    ccr._reap_started()
    assert ccr._started_proc is None


def test_reap_started_keeps_live_handle():
    """A still-running handle (poll() returns None) is kept."""
    live = _FakeProc(poll_value=None)
    ccr._started_proc = live
    ccr._reap_started()
    assert ccr._started_proc is live
    _reset_started()


def test_reap_started_noop_when_none():
    """No handle → nothing to do, stays None, never raises."""
    _reset_started()
    ccr._reap_started()
    assert ccr._started_proc is None


def test_spawn_env_reaps_finished_handle(monkeypatch):
    """spawn_env() reaps a finished start-handle at entry (zombie hygiene)."""
    ccr._started_proc = _FakeProc(poll_value=0)
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    s = _custom_settings()
    ccr.spawn_env(s, PROJECT_PATH, probe=lambda _s: True)
    assert ccr._started_proc is None


def test_sync_reaps_finished_handle(monkeypatch):
    """sync() reaps a finished start-handle at entry too."""
    ccr._started_proc = _FakeProc(poll_value=0)
    # No custom model active → sync returns early after the reap, no start.
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    s = _custom_settings(model_default='')
    monkeypatch.setattr(ccr, 'is_running', lambda _s: False)
    ccr.sync(s)
    assert ccr._started_proc is None


# ---------------------------------------------------------------------------
# spawn_env single-flight start — VM gate round-3 finding (double-start races
# ccr's pidfile; the bind-race loser deletes the winner's pidfile on exit,
# leaving the surviving server unstoppable by `ccr stop`)
# ---------------------------------------------------------------------------

def _probe_second_call_true():
    calls = [0]
    def probe(_s):
        calls[0] += 1
        return calls[0] > 1
    return probe


def test_spawn_env_skips_start_when_one_in_flight(monkeypatch):
    """A live detached start (e.g. sync()'s at app startup) must not be
    doubled; the poll alone decides readiness."""
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    starts = []
    monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
    monkeypatch.setattr(ccr, '_started_proc', _FakeProc(poll_value=None))
    env, reason = ccr.spawn_env(_custom_settings(), PROJECT_PATH,
                                probe=_probe_second_call_true(),
                                sleep_fn=lambda s: None)
    assert starts == [], 'must not double-start while one is in flight'
    assert reason is None
    assert env is not None


def test_spawn_env_starts_when_no_start_in_flight(monkeypatch):
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    starts = []
    monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
    monkeypatch.setattr(ccr, '_started_proc', None)
    env, reason = ccr.spawn_env(_custom_settings(), PROJECT_PATH,
                                probe=_probe_second_call_true(),
                                sleep_fn=lambda s: None)
    assert starts == [1]
    assert env is not None


def test_spawn_env_finished_start_reaped_then_restarted(monkeypatch):
    """A dead earlier start must not suppress a needed one — the entry reap
    clears the finished handle before the single-flight check."""
    monkeypatch.setattr(ccr, 'available', lambda _s: True)
    starts = []
    monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
    monkeypatch.setattr(ccr, '_started_proc', _FakeProc(poll_value=1))
    env, reason = ccr.spawn_env(_custom_settings(), PROJECT_PATH,
                                probe=_probe_second_call_true(),
                                sleep_fn=lambda s: None)
    assert starts == [1], 'finished handle reaped, then start fires'
    assert env is not None
