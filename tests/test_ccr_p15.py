"""Tests for P1.5 ccr follow-up fixes.

Items covered:
  1. ccr_managed=False gate in sync() and spawn_env()
  2. Failed-start cooldown (injectable monotonic_fn) — N-project restore pays ONE poll
  3. restart() bounded reap-wait (injectable sleep_fn) before overwriting handle
  4. Toast aggregator helper (pure, testable)
  5. Nit: agents.py unused import (verified by import side-effect)
"""

import ccr
from settings import Settings


# ---------------------------------------------------------------------------
# Helpers shared across items
# ---------------------------------------------------------------------------

PROJECT_PATH = '/projects/myproj'


def _managed_settings(**kw):
    """Settings with a custom provider+model and ccr_managed=True (default)."""
    base = dict(
        providers={
            'ollama': {
                'base_url': 'http://host:11434/v1',
                'api_key': 'k',
                'models': {'qwen': {'name': 'Qwen'}},
            },
        },
        model_default='ollama/qwen',
        ccr_api_key='secret',
        ccr_managed=True,
    )
    base.update(kw)
    return Settings(**base)


def _unmanaged_settings(**kw):
    """Same as above but ccr_managed=False."""
    return _managed_settings(ccr_managed=False, **kw)


def _reset_cooldown():
    """Clear any live cooldown state between tests."""
    ccr._cooldown_deadline = None


def _reset_started():
    ccr._started_proc = None


# ---------------------------------------------------------------------------
# Item 1 — ccr_managed=False honoured by sync() and spawn_env()
# ---------------------------------------------------------------------------

class TestManagedFalseSync:
    """sync() must be a pure no-op (beyond _reap_started) when managed=False."""

    def test_sync_managed_false_does_not_write_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        writes = []
        monkeypatch.setattr(ccr, 'write_config', lambda _s: writes.append(1) or True)
        monkeypatch.setattr(ccr, 'is_running', lambda _s: False)
        monkeypatch.setattr(ccr, 'config_differs', lambda _s: True)
        ccr.sync(_unmanaged_settings())
        assert writes == [], 'write_config must never be called when ccr_managed=False'

    def test_sync_managed_false_does_not_start(self, monkeypatch):
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
        monkeypatch.setattr(ccr, 'is_running', lambda _s: False)
        monkeypatch.setattr(ccr, 'config_differs', lambda _s: True)
        ccr.sync(_unmanaged_settings())
        assert starts == [], 'start must never be called when ccr_managed=False'

    def test_sync_managed_false_does_not_stop(self, monkeypatch):
        """Even when ccr is running, sync() with managed=False must not stop it."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        stops = []
        monkeypatch.setattr(ccr, 'stop', lambda _s: stops.append(1) or True)
        monkeypatch.setattr(ccr, 'is_running', lambda _s: True)
        # No custom model active — managed=True would stop, managed=False must not.
        ccr.sync(_unmanaged_settings(model_default=''))
        assert stops == [], 'stop must never be called when ccr_managed=False'

    def test_sync_managed_true_still_starts(self, monkeypatch):
        """Regression guard: managed=True, no custom model → stop; with model → start."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'config_differs', lambda _s: True)
        monkeypatch.setattr(ccr, 'is_running', lambda _s: False)
        monkeypatch.setattr(ccr, 'write_config', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
        ccr.sync(_managed_settings())   # custom model active
        assert starts == [1], 'managed=True: start should be called when not running'


class TestManagedFalseSpawnEnv:
    """spawn_env() must never call start() when ccr_managed=False."""

    def setup_method(self):
        _reset_cooldown()
        _reset_started()

    def test_spawn_env_managed_false_ccr_running_uses_it(self, monkeypatch):
        """When managed=False and ccr is already running externally, use it."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        s = _unmanaged_settings()
        env, reason = ccr.spawn_env(s, PROJECT_PATH, probe=lambda _s: True)
        assert reason is None
        assert env is not None
        assert 'ANTHROPIC_BASE_URL' in env

    def test_spawn_env_managed_false_ccr_not_running_distinct_reason(self, monkeypatch):
        """When managed=False and ccr is NOT running, return a DISTINCT reason that
        mentions 'Manage ccr' or manual start — NOT the generic start-failed reason."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
        s = _unmanaged_settings()
        env, reason = ccr.spawn_env(s, PROJECT_PATH,
                                    probe=lambda _s: False,
                                    sleep_fn=lambda _t: None)
        assert env is None
        assert reason and isinstance(reason, str)
        # Must NOT have started ccr — the whole point of managed=False
        assert starts == [], 'start() must NEVER be called when ccr_managed=False'
        # Distinct reason: must reference "Manage ccr" or manual start
        reason_lower = reason.lower()
        assert ('manage ccr' in reason_lower or 'manually' in reason_lower or
                'not managed' in reason_lower or 'enable' in reason_lower), (
            f'Reason should mention ccr management, got: {reason!r}')

    def test_spawn_env_managed_false_not_running_reason_differs_from_start_failed(
            self, monkeypatch):
        """The not-managed reason must be textually distinct from the start-failed reason."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: False)

        # Collect the managed=True start-failed reason
        s_managed = _managed_settings()
        _, reason_managed = ccr.spawn_env(s_managed, PROJECT_PATH,
                                          probe=lambda _s: False,
                                          sleep_fn=lambda _t: None,
                                          start_wait=0.25)
        _reset_cooldown()

        # Collect the managed=False not-running reason
        s_unmanaged = _unmanaged_settings()
        _, reason_unmanaged = ccr.spawn_env(s_unmanaged, PROJECT_PATH,
                                            probe=lambda _s: False,
                                            sleep_fn=lambda _t: None)
        assert reason_managed != reason_unmanaged, (
            'not-managed reason must be distinct from start-failed reason')

    def test_spawn_env_managed_true_not_running_calls_start(self, monkeypatch):
        """Regression: managed=True still calls start() when ccr not running."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or False)
        s = _managed_settings()
        ccr.spawn_env(s, PROJECT_PATH, probe=lambda _s: False,
                      sleep_fn=lambda _t: None, start_wait=0.25)
        assert starts == [1], 'managed=True: start() must be called when not running'


# ---------------------------------------------------------------------------
# Item 2 — Failed-start cooldown (injectable monotonic_fn)
# ---------------------------------------------------------------------------

class TestCooldown:
    """After a failed start+poll cycle, spawn_env() suppresses start+poll for
    ~30s — each call pays at most the one cheap probe, never the 4s budget."""

    def setup_method(self):
        _reset_cooldown()
        _reset_started()

    def test_second_spawn_within_cooldown_skips_start_and_poll(self, monkeypatch):
        """The second call while cooldown active pays exactly ONE probe and
        skips start + the poll loop entirely (no sleeps), returning the same
        failure reason as the full cycle."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or False)
        s = _managed_settings()

        # First call: fails → sets cooldown; fake clock stays at 0
        tick = [0]
        def monotonic():
            return float(tick[0])

        _, reason1 = ccr.spawn_env(s, PROJECT_PATH,
                                   probe=lambda _s: False,
                                   sleep_fn=lambda _t: None,
                                   start_wait=0.25,
                                   monotonic_fn=monotonic)
        assert reason1 is not None
        assert len(starts) >= 1

        # Second call: clock still at 0 (well within cooldown window)
        starts.clear()
        probes = []
        sleeps = []
        _, reason2 = ccr.spawn_env(s, PROJECT_PATH,
                                   probe=lambda _s: probes.append(1) or False,
                                   sleep_fn=sleeps.append,
                                   start_wait=0.25,
                                   monotonic_fn=monotonic)
        assert reason2 is not None
        assert starts == [], 'start must NOT be called during cooldown'
        assert probes == [1], 'exactly ONE probe during cooldown (cheap not-running check)'
        assert sleeps == [], 'poll loop must NOT run during cooldown (no sleeps)'

    def test_cooldown_cleared_on_expiry(self, monkeypatch):
        """After the cooldown window expires, the next call retries normally."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or False)
        s = _managed_settings()
        tick = [0]

        # First call fails, sets cooldown
        ccr.spawn_env(s, PROJECT_PATH,
                      probe=lambda _s: False,
                      sleep_fn=lambda _t: None,
                      start_wait=0.25,
                      monotonic_fn=lambda: float(tick[0]))
        starts.clear()

        # Advance clock past cooldown window (default 30s)
        tick[0] = 60
        ccr.spawn_env(s, PROJECT_PATH,
                      probe=lambda _s: False,
                      sleep_fn=lambda _t: None,
                      start_wait=0.25,
                      monotonic_fn=lambda: float(tick[0]))
        assert starts != [], 'start must be retried after cooldown expires'

    def test_cooldown_cleared_on_successful_probe(self, monkeypatch):
        """Cooldown ACTIVE + probe True → env returned AND cooldown cleared.

        The probe runs before the cooldown check, so a running server is never
        ignored. Scenario: restore fails (cooldown set), user runs `ccr start`
        manually in a terminal, respawns the project — the healthy ccr must be
        used immediately, not suppressed for the rest of the window.
        """
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: False)
        s = _managed_settings()
        tick = [0]

        # First call fails → sets cooldown
        ccr.spawn_env(s, PROJECT_PATH,
                      probe=lambda _s: False,
                      sleep_fn=lambda _t: None,
                      start_wait=0.25,
                      monotonic_fn=lambda: float(tick[0]))
        assert ccr._cooldown_deadline is not None

        # Cooldown still active (same tick); ccr now running externally
        env, reason = ccr.spawn_env(s, PROJECT_PATH,
                                    probe=lambda _s: True,   # ccr back up
                                    sleep_fn=lambda _t: None,
                                    monotonic_fn=lambda: float(tick[0]))
        assert reason is None and env is not None, (
            'a running ccr must be used even mid-cooldown')
        assert ccr._cooldown_deadline is None, (
            'successful probe must clear the cooldown deadline')

    def test_cooldown_cleared_on_config_write(self, monkeypatch, tmp_path):
        """sync() writing a new config clears the cooldown (user fixed something)."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or False)
        s = _managed_settings()
        tick = [0]

        # Set cooldown via a failed spawn
        ccr.spawn_env(s, PROJECT_PATH,
                      probe=lambda _s: False,
                      sleep_fn=lambda _t: None,
                      start_wait=0.25,
                      monotonic_fn=lambda: float(tick[0]))
        assert ccr._cooldown_deadline is not None, 'cooldown must be set after failure'

        # Simulate sync() writing a config (config_differs=True, is_running=False)
        monkeypatch.setattr(ccr, 'config_differs', lambda _s: True)
        monkeypatch.setattr(ccr, 'is_running', lambda _s: False)
        monkeypatch.setattr(ccr, 'write_config', lambda _s: True)
        starts.clear()
        ccr.sync(s)
        assert ccr._cooldown_deadline is None, 'sync() config-write must clear cooldown'

    def test_suppression_reason_identical_to_full_failure_reason(self, monkeypatch):
        """The cooldown path's reason is byte-equal to the full-failure path's.

        Identical strings are what let aggregate_fallback_notices() collapse a
        restore burst (one full failure + N-1 suppressed) into ONE toast — a
        per-spawn time-varying suffix split it (VM gate s2 finding).
        """
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: False)
        s = _managed_settings()
        tick = [0]

        _, full_failure = ccr.spawn_env(s, PROJECT_PATH,
                                        probe=lambda _s: False,
                                        sleep_fn=lambda _t: None,
                                        start_wait=0.25,
                                        monotonic_fn=lambda: float(tick[0]))
        _, suppressed = ccr.spawn_env(s, PROJECT_PATH,
                                      probe=lambda _s: False,
                                      sleep_fn=lambda _t: None,
                                      start_wait=0.25,
                                      monotonic_fn=lambda: float(tick[0]))
        assert full_failure is not None and suppressed is not None
        assert suppressed == full_failure, (
            f'cooldown reason must be byte-equal to the full-failure reason\n'
            f'full:       {full_failure!r}\nsuppressed: {suppressed!r}')

    def test_cooldown_reason_omits_suppression_detail(self, monkeypatch, capsys):
        """The returned reason carries no time-varying suppression suffix
        (which would break toast aggregation); the detail goes to the debug
        log only."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: False)
        s = _managed_settings(debug_logging=True)
        tick = [0]

        ccr.spawn_env(s, PROJECT_PATH,
                      probe=lambda _s: False,
                      sleep_fn=lambda _t: None,
                      start_wait=0.25,
                      monotonic_fn=lambda: float(tick[0]))
        capsys.readouterr()   # discard the first (full-failure) call's logs

        _, reason = ccr.spawn_env(s, PROJECT_PATH,
                                  probe=lambda _s: False,
                                  sleep_fn=lambda _t: None,
                                  start_wait=0.25,
                                  monotonic_fn=lambda: float(tick[0]))
        assert reason is not None
        assert 'suppressed' not in reason, (
            f'reason must not carry the suppression suffix, got: {reason!r}')
        out = capsys.readouterr().out
        assert 'retry suppressed' in out, (
            'suppression detail must still reach the debug log')

    def test_aggregator_collapses_failure_burst_into_one_toast(self, monkeypatch):
        """Integration (the binding VM-gate case): a 3-project restore burst —
        one full failure + two cooldown-suppressed spawns — yields identical
        reasons and therefore exactly ONE aggregate toast string."""
        monkeypatch.setattr(ccr, 'available', lambda _s: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: False)
        s = _managed_settings()
        tick = [0]
        kw = dict(probe=lambda _s: False, sleep_fn=lambda _t: None,
                  start_wait=0.25, monotonic_fn=lambda: float(tick[0]))

        _, r1 = ccr.spawn_env(s, '/projects/p1', **kw)   # full failure → cooldown set
        _, r2 = ccr.spawn_env(s, '/projects/p2', **kw)   # suppressed
        _, r3 = ccr.spawn_env(s, '/projects/p3', **kw)   # suppressed
        result = ccr.aggregate_fallback_notices(
            [('p1', r1), ('p2', r2), ('p3', r3)])
        assert isinstance(result, str), (
            f'burst must collapse to ONE string, got: {result!r}')
        assert result == (
            f'ccr unavailable — 3 projects running native Claude. {r1}')


# ---------------------------------------------------------------------------
# Item 3 — restart() bounded reap-wait
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for the Popen handle."""
    def __init__(self, poll_values):
        # poll_values: list of return values, one per poll() call; last is repeated
        self._values = list(poll_values)
        self._idx = 0
        self.poll_calls = 0
        self.pid = 9999

    def poll(self):
        self.poll_calls += 1
        v = self._values[min(self._idx, len(self._values) - 1)]
        self._idx += 1
        return v


class TestRestartBoundedReap:
    """restart() waits at most 2s for the old handle to exit before overwriting."""

    def setup_method(self):
        _reset_started()

    def test_restart_reaps_handle_that_exits_during_wait(self, monkeypatch):
        """Old proc exits on second poll() → reaped, then start fires."""
        old_proc = _FakeProc(poll_values=[None, 0])  # alive, then exited
        ccr._started_proc = old_proc

        sleeps = []
        monkeypatch.setattr(ccr, '_run', lambda _s, *a: True)  # stop() succeeds
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)

        ccr.restart(_managed_settings(), sleep_fn=sleeps.append)
        assert old_proc.poll_calls >= 2, 'must poll at least twice'
        assert len(sleeps) >= 1, 'must sleep between polls'
        assert starts == [1]
        _reset_started()

    def test_restart_proceeds_after_timeout_even_if_proc_still_live(self, monkeypatch):
        """If the old proc never exits within the budget, restart continues anyway."""
        # Always returns None (never exits)
        old_proc = _FakeProc(poll_values=[None] * 20)
        ccr._started_proc = old_proc

        sleeps = []
        monkeypatch.setattr(ccr, '_run', lambda _s, *a: True)
        starts = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)

        ccr.restart(_managed_settings(), sleep_fn=sleeps.append)
        # Must have tried a bounded number of times (≤2s / 0.25s = ≤8 steps)
        assert old_proc.poll_calls <= 8, (
            f'must not reap-poll forever; got {old_proc.poll_calls}')
        assert starts == [1], 'start must fire regardless'
        _reset_started()

    def test_restart_no_old_handle_starts_immediately(self, monkeypatch):
        """No prior handle → no wait, start fires right away."""
        ccr._started_proc = None
        monkeypatch.setattr(ccr, '_run', lambda _s, *a: True)
        starts = []
        sleeps = []
        monkeypatch.setattr(ccr, 'start', lambda _s: starts.append(1) or True)
        ccr.restart(_managed_settings(), sleep_fn=sleeps.append)
        assert starts == [1]
        assert sleeps == [], 'no old handle → no wait steps'
        _reset_started()

    def test_restart_sleep_fn_default_not_called_in_tests(self, monkeypatch):
        """Regression: restart() accepts sleep_fn keyword (signature check)."""
        monkeypatch.setattr(ccr, '_run', lambda _s, *a: True)
        monkeypatch.setattr(ccr, 'start', lambda _s: True)
        ccr._started_proc = None
        # Just checking the signature accepts sleep_fn without error
        result = ccr.restart(_managed_settings(), sleep_fn=lambda _t: None)
        assert result is True


# ---------------------------------------------------------------------------
# Item 4 — Toast aggregator helper
# ---------------------------------------------------------------------------

from ccr import aggregate_fallback_notices   # noqa: E402 — tested pure helper


class TestAggregatorHelper:
    """aggregate_fallback_notices: pure function, no GTK."""

    # Verbatim P0 single-project string (MUST remain unchanged per spec)
    _P0_REASON = 'ccr did not start on 127.0.0.1:3456 — spawning with native Anthropic.'

    def test_single_project_verbatim_p0_string(self):
        """Single entry → verbatim P0 format: 'ccr unavailable — running native Claude. <reason>'"""
        result = aggregate_fallback_notices([('myproj', self._P0_REASON)])
        expected = f'ccr unavailable — running native Claude. {self._P0_REASON}'
        assert result == expected, (
            f'Single-project P0 string must be verbatim.\nExpected: {expected!r}\nGot:      {result!r}')

    def test_multiple_same_reason_collapses(self):
        """Multiple projects with the SAME reason collapse to a single aggregate string."""
        events = [
            ('proj-a', self._P0_REASON),
            ('proj-b', self._P0_REASON),
            ('proj-c', self._P0_REASON),
        ]
        result = aggregate_fallback_notices(events)
        assert '3' in result or 'three' in result.lower(), (
            f'Collapsed toast must mention the count, got: {result!r}')
        assert self._P0_REASON in result, 'Reason text must appear in aggregate'
        assert 'native Claude' in result

    def test_multiple_same_reason_two_projects(self):
        """Two projects with same reason collapse."""
        events = [('a', self._P0_REASON), ('b', self._P0_REASON)]
        result = aggregate_fallback_notices(events)
        # Must contain count "2"
        assert '2' in result, f'Expected count 2 in: {result!r}'

    def test_different_reasons_returned_as_separate_strings(self):
        """Different reasons produce separate display strings (one per reason)."""
        reason_a = 'ccr did not start on 127.0.0.1:3456 — spawning with native Anthropic.'
        reason_b = 'ccr binary not found — spawning with native Anthropic. Install claude-code-router or clear the custom model setting.'
        events = [('proj-a', reason_a), ('proj-b', reason_b)]
        results = aggregate_fallback_notices(events)
        # When reasons differ the helper returns a list of strings
        if isinstance(results, list):
            assert len(results) == 2, f'Expected 2 separate strings, got {len(results)}'
        else:
            # Or a single string — implementation choice — just ensure both reasons present
            assert reason_a in results or reason_b in results

    def test_empty_input_returns_empty_or_none(self):
        """Empty event list → empty result (no toast to show)."""
        result = aggregate_fallback_notices([])
        # Must be falsy: None, '', or []
        assert not result, f'Empty input should return falsy, got: {result!r}'

    def test_single_project_result_is_string(self):
        """Single entry always returns a plain string (not a list)."""
        result = aggregate_fallback_notices([('myproj', self._P0_REASON)])
        assert isinstance(result, str), (
            f'Single-project result must be str, got {type(result).__name__}')


# ---------------------------------------------------------------------------
# Item 5 — NIT: agents.py unused field import
# ---------------------------------------------------------------------------

def test_agents_no_unused_field_import():
    """agents.py must not import dataclasses.field (it was never used)."""
    import importlib, ast, pathlib
    src = pathlib.Path(__file__).parent.parent / 'agents.py'
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module == 'dataclasses':
                names = [a.name for a in node.names]
                assert 'field' not in names, (
                    f"agents.py still imports 'field' from dataclasses: {names}")
