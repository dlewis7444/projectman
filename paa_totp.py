"""TOTP (RFC 6238) for the PAA Telegram bot's approval confirmations.

Stdlib only (hmac, hashlib, base64, secrets, struct, time, subprocess).
The bot imports the functions directly; ``main()`` is a setup/debug CLI:

* ``python paa_totp.py --setup`` — generate a fresh secret, print it and the
  otpauth:// URI, and tell the operator to store the secret in ``pass``.
* ``python paa_totp.py --verify CODE`` — read the secret from
  ``pass show external/telegram/paa-bot-totp`` and check CODE. Exit 0 on
  match, 1 on mismatch, 2 on operational errors. The secret is never
  printed.

The module never invokes ``pass`` except inside ``--verify``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import subprocess
import sys
import time
import urllib.parse

_PASS_ENTRY = 'external/telegram/paa-bot-totp'
_DIGITS = 6
_STEP_SEC = 30
_T0 = 0


def generate_secret() -> str:
    """20 random bytes, base32 without padding (32 chars), authenticator-ready."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _decode_secret(secret: str) -> bytes:
    cleaned = ''.join(str(secret).split()).upper()
    pad = '=' * ((8 - len(cleaned) % 8) % 8)
    return base64.b32decode(cleaned + pad)


def _hotp(secret: str | bytes, counter: int, digits: int = _DIGITS) -> str:
    # Accept the decoded key bytes (verify decodes once) as well as the
    # base32 string form used by the setup/debug CLI.
    key = secret if isinstance(secret, bytes) else _decode_secret(secret)
    digest = hmac.new(
        key, struct.pack('>Q', counter), hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f'{code % (10 ** digits):0{digits}d}'


def totp_uri(secret: str, issuer: str = 'ProjectMan PAA',
             account: str = 'paa-telegram') -> str:
    """otpauth:// URI an authenticator app can import."""
    label = urllib.parse.quote(f'{issuer}:{account}', safe='')
    query = urllib.parse.urlencode({'secret': secret, 'issuer': issuer})
    return f'otpauth://totp/{label}?{query}'


def verify(secret: str, code, *, now=None, window: int = 1) -> bool:
    """True when *code* matches the TOTP for *secret* within ±*window* steps.

    Whitespace in the typed code is ignored. Comparison is constant-time.
    ``now`` defaults to ``time.time()``; inject a float for tests. Never
    raises on bad input: a secret that does not base32-decode collapses
    into the same opaque False as a wrong code.
    """
    if now is None:
        now = time.time()
    try:
        typed = ''.join(str(code).split())
    except Exception:  # noqa: BLE001 — un-typable input simply fails
        return False
    if not typed or not typed.isascii() or not typed.isdigit():
        return False
    try:
        key = _decode_secret(secret)
    except (binascii.Error, ValueError):  # malformed secret == opaque failure
        return False
    counter = int((float(now) - _T0) // _STEP_SEC)
    for candidate in range(counter - int(window), counter + int(window) + 1):
        if candidate < 0:
            continue
        if hmac.compare_digest(_hotp(key, candidate), typed):
            return True
    return False


class Throttle:
    """Per-identity failed-attempt throttle (in-memory, per-process).

    After *max_attempts* consecutive failures (default 3) the identity is
    locked out for *cooldown_sec* (default 300); :meth:`allow` stays False
    until the cooldown expires, at which point the failure record resets and
    the identity gets a fresh run of attempts. :meth:`record_success` clears
    the record immediately. The clock is injectable via *now_fn* so tests
    can step time without sleeping.
    """

    def __init__(self, *, max_attempts: int = 3, cooldown_sec: float = 300,
                 now_fn=None):
        if max_attempts < 1:
            raise ValueError('max_attempts must be >= 1')
        self.max_attempts = int(max_attempts)
        self.cooldown_sec = float(cooldown_sec)
        self._now = now_fn or time.time
        self._failures: dict = {}

    def _at(self, now):
        return self._now() if now is None else float(now)

    def allow(self, identity, now=None) -> bool:
        """True when *identity* may attempt (not in an active cooldown)."""
        rec = self._failures.get(identity)
        if rec is None:
            return True
        count, locked_until = rec
        if locked_until is None:
            return True
        if self._at(now) >= locked_until:
            del self._failures[identity]
            return True
        return False

    def record_failure(self, identity, now=None) -> None:
        """Register one failure; trip the cooldown at *max_attempts*."""
        at = self._at(now)
        count, _until = self._failures.get(identity, (0, None))
        count += 1
        locked_until = at + self.cooldown_sec if count >= self.max_attempts \
            else None
        self._failures[identity] = (count, locked_until)

    def record_success(self, identity, now=None) -> None:
        """Clear the failure record — a verified attempt forgives the rest."""
        self._failures.pop(identity, None)


def _read_pass_secret(entry: str = _PASS_ENTRY) -> str:
    proc = subprocess.run(
        ['pass', 'show', entry], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'pass show {entry} failed: {proc.stderr.strip()}')
    first_line = (proc.stdout or '').splitlines()
    if not first_line or not first_line[0].strip():
        raise RuntimeError(f'pass entry {entry} is empty')
    return first_line[0].strip()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--setup']:
        secret = generate_secret()
        print(secret)
        print(totp_uri(secret))
        print()
        print('Store the FIRST line (the base32 secret) in pass:')
        print(f'    pass insert {_PASS_ENTRY}')
        print('This CLI never reads or prints the stored secret again.')
        return 0
    if len(argv) == 2 and argv[0] == '--verify':
        try:
            secret = _read_pass_secret()
        except (RuntimeError, OSError) as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 2
        ok = verify(secret, argv[1])
        print('ok' if ok else 'mismatch')
        return 0 if ok else 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
