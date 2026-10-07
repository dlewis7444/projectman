"""Unit tests for paa_totp — RFC 6238 vectors, windowing, throttle, CLI."""
import re
import subprocess
import time
from unittest.mock import MagicMock

import pytest

import paa_totp
from paa_totp import Throttle, generate_secret, main, totp_uri, verify

# RFC 6238 Appendix B SHA1 test secret (ASCII "12345678901234567890").
RFC_SECRET_B32 = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
# (unix_time, expected 6-digit code) — the 8-digit RFC vectors, truncated.
RFC_VECTORS_6 = [
    (59, '287082'),
    (1111111109, '081804'),
    (1111111111, '050471'),
    (1234567890, '005924'),
    (2000000000, '279037'),
    (20000000000, '353130'),
]


class TestRfcVectors:
    @pytest.mark.parametrize('now,expected', RFC_VECTORS_6)
    def test_rfc_6238_sha1_vectors(self, now, expected):
        assert verify(RFC_SECRET_B32, expected, now=now, window=0)

    @pytest.mark.parametrize('now,expected', RFC_VECTORS_6)
    def test_rfc_vectors_reject_with_zero_window_at_other_steps(self, now,
                                                                expected):
        # A code from a neighbouring step must not verify at this step.
        assert not verify(RFC_SECRET_B32, expected, now=now + 30, window=0)

    def test_reference_codes_match_module(self):
        # Pin the RFC vectors against the module's own HOTP so a future
        # refactor cannot drift from the standard silently.
        for now, expected in RFC_VECTORS_6:
            counter = now // 30
            assert paa_totp._hotp(RFC_SECRET_B32, counter) == expected


class TestVerifyWindow:
    def test_window_accepts_previous_and_next_step(self):
        now = 1_111_111_111
        good = paa_totp._hotp(RFC_SECRET_B32, now // 30)
        prev = paa_totp._hotp(RFC_SECRET_B32, now // 30 - 1)
        nxt = paa_totp._hotp(RFC_SECRET_B32, now // 30 + 1)
        assert verify(RFC_SECRET_B32, prev, now=now, window=1)
        assert verify(RFC_SECRET_B32, good, now=now, window=1)
        assert verify(RFC_SECRET_B32, nxt, now=now, window=1)

    def test_window_rejects_two_steps_away(self):
        now = 1_111_111_111
        far = paa_totp._hotp(RFC_SECRET_B32, now // 30 + 2)
        assert not verify(RFC_SECRET_B32, far, now=now, window=1)

    def test_window_zero_is_exact_step_only(self):
        now = 1_111_111_111
        exact = paa_totp._hotp(RFC_SECRET_B32, now // 30)
        assert verify(RFC_SECRET_B32, exact, now=now, window=0)
        neighbour = paa_totp._hotp(RFC_SECRET_B32, now // 30 - 1)
        assert not verify(RFC_SECRET_B32, neighbour, now=now, window=0)

    def test_whitespace_is_tolerated(self):
        now = 59
        assert verify(RFC_SECRET_B32, ' 287 082 ', now=now, window=0)
        assert verify(RFC_SECRET_B32, '287082\n', now=now, window=0)

    @pytest.mark.parametrize('code', ['', '   ', 'abcdef', '12345', '1234567',
                                      None, '１２３４５６'])
    def test_garbage_codes_rejected(self, code):
        assert not verify(RFC_SECRET_B32, code, now=59, window=1)

    def test_wrong_secret_rejects(self):
        other = generate_secret()
        assert not verify(other, '287082', now=59, window=1)

    @pytest.mark.parametrize('secret', [
        '####not-base32####', 'MZXW6===junk', 'A!', '', None, 12345,
        'gezdgnbvgy3tqojq gezdgnbvgy3tqojq!',  # padding trickery, still bad
    ])
    def test_garbage_secret_returns_false_and_never_raises(self, secret):
        # A malformed secret must collapse into the same opaque False as a
        # wrong code: the bot's poll loop must never see an exception here.
        assert verify(secret, '287082', now=59, window=1) is False

    def test_malformed_secret_with_garbage_code_still_false(self):
        assert not verify('####bad####', None, now=59, window=1)
        assert not verify('####bad####', '１２３４５６', now=59, window=1)


class TestGenerateSecret:
    def test_charset_and_length(self):
        for _ in range(25):
            secret = generate_secret()
            assert len(secret) == 32
            assert re.fullmatch(r'[A-Z2-7]+', secret)

    def test_secrets_are_unique(self):
        assert len({generate_secret() for _ in range(50)}) == 50


class TestTotpUri:
    def test_uri_shape(self):
        secret = generate_secret()
        uri = totp_uri(secret)
        assert uri.startswith('otpauth://totp/')
        assert 'ProjectMan%20PAA%3Apaa-telegram' in uri
        assert f'secret={secret}' in uri
        assert 'issuer=ProjectMan+PAA' in uri.replace('%20', '+')

    def test_uri_custom_parts_are_quoted(self):
        uri = totp_uri('ABC', issuer='My Org/PAA', account='bot@example.com')
        assert 'My%20Org%2FPAA%3Abot%40example.com' in uri
        assert 'issuer=My+Org%2FPAA' in uri.replace('%2F', '%2F')


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class TestThrottle:
    def test_allows_when_untouched(self):
        th = Throttle(now_fn=FakeClock())
        assert th.allow('user:1') is True

    def test_locks_out_after_max_attempts_and_cooldown_expires(self):
        clock = FakeClock()
        th = Throttle(now_fn=clock)
        for _ in range(3):
            th.record_failure('user:1')
        assert th.allow('user:1') is False
        clock.t += 299
        assert th.allow('user:1') is False
        clock.t += 2  # past the 300s cooldown
        assert th.allow('user:1') is True
        # Cooldown expiry reset the record: a fresh run of attempts.
        th.record_failure('user:1')
        th.record_failure('user:1')
        assert th.allow('user:1') is True

    def test_success_clears_failures(self):
        clock = FakeClock()
        th = Throttle(now_fn=clock)
        th.record_failure('user:1')
        th.record_failure('user:1')
        th.record_success('user:1')
        th.record_failure('user:1')
        th.record_failure('user:1')
        assert th.allow('user:1') is True  # only 2 consecutive, no lockout

    def test_identities_are_isolated(self):
        th = Throttle(now_fn=FakeClock())
        for _ in range(3):
            th.record_failure('user:1')
        assert th.allow('user:2') is True

    def test_injected_now_overrides_clock(self):
        clock = FakeClock()
        th = Throttle(now_fn=clock)
        for _ in range(3):
            th.record_failure('user:1')
        assert th.allow('user:1', now=clock.t + 301) is True

    def test_rejects_bad_config(self):
        with pytest.raises(ValueError):
            Throttle(max_attempts=0)


class TestCli:
    def test_setup_prints_secret_and_pass_instruction(self, capsys):
        assert main(['--setup']) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert re.fullmatch(r'[A-Z2-7]{32}', lines[0])
        assert lines[1].startswith('otpauth://totp/')
        assert 'pass insert external/telegram/paa-bot-totp' in out

    def test_verify_ok_exits_zero_and_never_prints_secret(self, monkeypatch,
                                                          capsys):
        from paa_totp import _PASS_ENTRY
        secret = generate_secret()
        code = paa_totp._hotp(secret, int(time.time() // 30))
        calls = []
        fake = MagicMock(returncode=0, stdout=secret + '\n', stderr='')

        def _run(argv, **kwargs):
            calls.append((argv, kwargs))
            return fake

        monkeypatch.setattr(subprocess, 'run', _run)
        assert main(['--verify', code]) == 0
        out = capsys.readouterr().out
        assert secret not in out
        assert calls[0][0] == ['pass', 'show', _PASS_ENTRY]

    def test_verify_mismatch_exits_one(self, monkeypatch, capsys):
        secret = generate_secret()
        monkeypatch.setattr(
            subprocess, 'run',
            lambda *a, **k: MagicMock(returncode=0, stdout=secret + '\n',
                                      stderr=''))
        assert main(['--verify', '000000']) == 1
        assert secret not in capsys.readouterr().out

    def test_verify_pass_failure_exits_two(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, 'run',
            lambda *a, **k: MagicMock(returncode=1, stdout='',
                                      stderr='gpg failed'))
        assert main(['--verify', '123456']) == 2

    def test_usage_error_exits_two(self, capsys):
        assert main([]) == 2
        assert main(['--nope']) == 2
        assert 'pass' in capsys.readouterr().err
