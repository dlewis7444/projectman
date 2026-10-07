"""Session owner for one resumable PAA conversation.

Telegram is a transport. This module stores the harness session id, runs one
chat turn at a time, and writes each accepted update to disk before the
offset is acknowledged. It does not attach to the Discuss VTE.
"""
from __future__ import annotations

import binascii
import json
import logging
import math
import os
import socket
import stat
import threading
import time
from dataclasses import dataclass

from paa_choices import PAGE_SIZE, ChoiceBook
from paa_findings import (
    discuss_finding_prompt,
    finding_notice_text,
    findings_page_text,
)
from paa_headless import HeadlessHandle
from paa_totp import Throttle, _decode_secret, _read_pass_secret, verify

log = logging.getLogger('paa_turns')

DEFAULT_PASS_ENTRY = 'internal/telegram/paa-bot-token'
CHAT_TIMEOUT_SEC = 600
TOTP_DEFAULT_ENTRY = 'external/telegram/paa-bot-totp'
# After a failed ``pass show`` do not retry for this long: pinentry can
# block, and every /unlock must not re-stall the poll thread.
SECRET_RETRY_SEC = 60.0
UNLOCK_SECONDS_MIN = 1
UNLOCK_SECONDS_MAX_RO = 999999
UNLOCK_SECONDS_MAX_RW = 86400

_COMMANDS = frozenset({
    'new', 'stop', 'status', 'help',
    'findings', 'ack', 'dismiss', 'discuss',
    'unlock', 'lock',
})

# Allowed values for the --reasoning-effort tiers (reviewer_effort and
# chat_effort). 'none' means "no flag at all".
EFFORT_TIERS = ('none', 'minimal', 'low', 'medium', 'high')

LOCKED_REPLY = 'PAA is locked'
UNLOCK_FAIL_REPLY = 'unable to unlock'
RW_REFUSAL = ('rw mode is unavailable: the MCP review channel is not '
              'running, so this turn was not run.')


@dataclass
class Outbound:
    kind: str
    chat_id: int | None = None
    text: str = ''
    reply_markup: dict | None = None
    message_id: int | None = None
    callback_query_id: str | None = None
    answer_text: str | None = None
    choice_group: str | None = None
    user_id: int | None = None
    # kind 'follow' is not a Telegram call. The poll loop sends every earlier
    # action first, then runs this on another thread so /stop can arrive.
    follow: str | None = None
    follow_prompt: str | None = None
    follow_policy: str | None = None


_OUT_FIELDS = (
    'kind', 'chat_id', 'text', 'reply_markup', 'message_id',
    'callback_query_id', 'answer_text', 'choice_group', 'user_id',
    'follow', 'follow_prompt', 'follow_policy',
)


def default_state_dir() -> str:
    override = os.environ.get('PAA_STATE_DIR')
    if override:
        return override
    return os.path.expanduser('~/.ProjectMan')


def load_bot_config(path: str) -> dict:
    """Bot config. A token key in the file is ignored."""
    cfg = {
        'allowed_user_ids': [],
        'pass_entry': DEFAULT_PASS_ENTRY,
        'chat_timeout_sec': CHAT_TIMEOUT_SEC,
        'totp_pass_entry': TOTP_DEFAULT_ENTRY,
        'findings_notices': False,
        'reviewer_effort': 'low',
        'chat_effort': 'medium',
    }
    try:
        with open(path, 'r') as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get('allowed_user_ids'), list):
        ids = []
        for item in data['allowed_user_ids']:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        cfg['allowed_user_ids'] = ids
    entry = data.get('pass_entry')
    if isinstance(entry, str) and entry.strip():
        cfg['pass_entry'] = entry.strip()
    totp_entry = data.get('totp_pass_entry')
    if isinstance(totp_entry, str) and totp_entry.strip():
        cfg['totp_pass_entry'] = totp_entry.strip()
    timeout = data.get('chat_timeout_sec')
    if isinstance(timeout, int) and timeout > 0:
        cfg['chat_timeout_sec'] = timeout
    notices = data.get('findings_notices')
    if isinstance(notices, bool):
        cfg['findings_notices'] = notices
    # Reviewer reasoning effort (2026-10-01 hardening, the maintainer-approved):
    # threaded to the gate reviewer's headless argv (grok
    # --reasoning-effort). Anything outside the allowed set falls back
    # to 'low' — an invalid value must never break the review channel.
    effort = data.get('reviewer_effort')
    if effort is not None:
        effort = str(effort).strip().lower()
        if effort in EFFORT_TIERS:
            cfg['reviewer_effort'] = effort
        else:
            log.warning('paa turns: invalid reviewer_effort %r; using low',
                        data.get('reviewer_effort'))
    # Main-turn reasoning effort (the maintainer 2026-10-01): threaded to the
    # user's chat turns as reasoning_effort. Same tier set; invalid
    # falls back to 'medium' + logged.
    effort = data.get('chat_effort')
    if effort is not None:
        effort = str(effort).strip().lower()
        if effort in EFFORT_TIERS:
            cfg['chat_effort'] = effort
        else:
            log.warning('paa turns: invalid chat_effort %r; using medium',
                        data.get('chat_effort'))
    return cfg


def format_turn_reply(result) -> str:
    """Reply body. Fallback reason is the first line when set."""
    lines = []
    if getattr(result, 'fallback_reason', None):
        lines.append(result.fallback_reason)
    text = getattr(result, 'text', None)
    error = getattr(result, 'error', None)
    if text:
        lines.append(text)
    elif error:
        lines.append(error)
    else:
        lines.append('')
    lines.append(f'Tokens: {getattr(result, "tokens", 0)}')
    return '\n'.join(lines)


def parse_command(text: str):
    """Return ``(name, arg)`` for a known command, else ``(None, None)``.

    Anything else, including an unknown slash word, is a prompt. A
    ``callback_query`` never reaches this parser.
    """
    if not isinstance(text, str) or not text.startswith('/'):
        return None, None
    parts = text.split(maxsplit=1)
    name = parts[0][1:]
    name = name.split('@', 1)[0].lower()
    if name not in _COMMANDS:
        return None, None
    arg = parts[1].strip() if len(parts) > 1 else ''
    return name, arg


def parse_unlock_args(arg: str):
    """Strict ``/unlock`` grammar: ``code [ro|rw [seconds]]``.

    Returns ``(code, mode, seconds)`` or ``None`` for anything malformed.
    Mode without seconds takes the mode default (ro 3600, rw 600); seconds
    without a mode are rejected. The mode is lowercase-only and the seconds
    all-digits between 1 and 86400 inclusive; the code itself is not
    validated here — ``paa_totp.verify`` decides, so a bad code and bad
    grammar fail identically and opaquely.
    """
    if not isinstance(arg, str):
        return None
    tokens = arg.split()
    if not tokens:
        return None
    code = tokens[0]
    mode = 'ro'
    rest = tokens[1:]
    if rest:
        if rest[0] not in ('ro', 'rw'):
            return None
        mode = rest[0]
        rest = rest[1:]
    if rest:
        if not rest[0].isdigit():
            return None
        seconds = int(rest[0])
        rest = rest[1:]
    else:
        seconds = 3600 if mode == 'ro' else 600
    # Mode-dependent ceilings (2026-10-01, the maintainer): ro windows may run long
    # (~11.6 days) so re-auth is rare; rw stays on a short leash.
    seconds_max = (UNLOCK_SECONDS_MAX_RO if mode == 'ro'
                   else UNLOCK_SECONDS_MAX_RW)
    if not UNLOCK_SECONDS_MIN <= seconds <= seconds_max:
        return None
    if rest:
        return None
    return code, mode, seconds


class TurnService:
    """One worker, one session file, one spool."""

    def __init__(self, state_dir, settings, chat_turn_fn, *, ledger=None,
                 now_fn=None, approval_sock_provider=None,
                 unlock_secret_fn=None):
        self.state_dir = state_dir
        self.settings = settings
        self.chat_turn = chat_turn_fn
        self.ledger = ledger
        self._now = now_fn or time.time
        # Broker integration point: called at the start of each rw turn;
        # returns the approval socket path, or None when the broker is
        # unavailable (the turn is then refused, never run un-gated).
        self.approval_sock_provider = approval_sock_provider
        # The ApprovalBroker handle, set by paa_telegram.serve() after the
        # self-test; /status reports it. None until then.
        self.approval_broker = None
        self._unlock_secret_fn = unlock_secret_fn
        os.makedirs(state_dir, exist_ok=True)
        self.spool_dir = os.path.join(state_dir, 'paa-telegram-spool')
        os.makedirs(self.spool_dir, exist_ok=True)
        self.config_path = os.path.join(state_dir, 'paa-telegram.json')
        self.session_path = os.path.join(state_dir, 'paa-session.json')
        self.offset_path = os.path.join(state_dir, 'paa-telegram.offset')
        self.operator_log = os.path.join(state_dir, 'paa-telegram-operator.log')
        self.unlock_path = os.path.join(state_dir, 'paa-unlock.json')
        self.config = load_bot_config(self.config_path)
        self.choices = ChoiceBook(
            os.path.join(state_dir, 'paa-choices.json'), now_fn=now_fn)
        self.offset = self._read_offset()
        self.session_id = None
        self.harness_id = settings.effective_harness('')
        self._archive: list = []
        self._gen = 0
        self._load_session()
        self._unlock_secret = None
        self._unlock_secret_ok = False
        self._unlock_secret_retry_at = 0.0
        self._throttle = Throttle(now_fn=self._now)
        self._unlock_window = self._read_unlock_window()
        self._worker = threading.Lock()
        self._state_lock = threading.Lock()
        self._spool_lock = threading.Lock()
        self._running = False
        self._handle: HeadlessHandle | None = None
        self._next_mid = 0
        self._last_scan = 0.0
        self._findings_group = None

    # -- persistence -------------------------------------------------------

    def _read_offset(self) -> int:
        try:
            with open(self.offset_path, 'r') as fh:
                return int(fh.read().strip() or '0')
        except (OSError, ValueError):
            return 0

    def _write_offset(self):
        tmp = self.offset_path + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(str(self.offset))
        os.replace(tmp, self.offset_path)

    def _load_session(self):
        try:
            with open(self.session_path, 'r') as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        sid = data.get('session_id')
        self.session_id = sid or None
        if data.get('harness_id'):
            self.harness_id = data['harness_id']
        archive = data.get('archive')
        if isinstance(archive, list):
            self._archive = [str(x) for x in archive if x]

    def _save_session(self):
        payload = {
            'session_id': self.session_id,
            'harness_id': self.harness_id,
            'archive': self._archive[-10:],
            'updated': self._now_stamp(),
        }
        tmp = self.session_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(payload, fh, indent=2)
            fh.write('\n')
        os.replace(tmp, self.session_path)

    def _now_stamp(self) -> str:
        return str(int(self.choices._now()))

    # -- unlock window -----------------------------------------------------

    # The unlock window gates ALL inbound communication. State is a small
    # json ({mode, expires_at}) next to the session file so a service
    # restart keeps the window; expired or unreadable state loads as locked.
    # TOTP verification and throttling live in paa_totp.

    def load_unlock_secret(self) -> bool:
        """Read the TOTP secret (``pass show``, first line, never logged).

        Called at bot startup by the telegram adapter; also retried on the
        next unlock attempt when not yet loaded, so a cold gpg-agent at
        boot does not brick unlocking forever. On failure the operational
        error is logged (never the secret) and every unlock attempt fails
        opaquely. A secret that does not base32-decode is treated exactly
        like an unreadable one.
        """
        reader = self._unlock_secret_fn or self._read_secret_from_pass
        try:
            secret = reader()
        except Exception as exc:  # noqa: BLE001 — unreadable == locked
            self._unlock_secret = None
            self._unlock_secret_ok = False
            self._unlock_secret_retry_at = self._now() + SECRET_RETRY_SEC
            log.error('paa unlock: cannot read TOTP secret: %s', exc)
            return False
        if not isinstance(secret, str) or not secret.strip():
            self._unlock_secret = None
            self._unlock_secret_ok = False
            self._unlock_secret_retry_at = self._now() + SECRET_RETRY_SEC
            log.error('paa unlock: TOTP secret source returned nothing')
            return False
        stripped = secret.strip()
        try:
            _decode_secret(stripped)
        except (binascii.Error, ValueError):
            self._unlock_secret = None
            self._unlock_secret_ok = False
            self._unlock_secret_retry_at = self._now() + SECRET_RETRY_SEC
            log.error('paa unlock: TOTP secret does not base32-decode')
            return False
        self._unlock_secret = stripped
        self._unlock_secret_ok = True
        self._unlock_secret_retry_at = 0.0
        return True

    def _read_secret_from_pass(self) -> str:
        entry = self.config.get('totp_pass_entry') or TOTP_DEFAULT_ENTRY
        return _read_pass_secret(entry)

    def _get_unlock_secret(self):
        if not self._unlock_secret_ok:
            # A failed read may block on pinentry; do not re-stall the
            # poll thread on every /unlock while the retry window lasts.
            if self._now() >= self._unlock_secret_retry_at:
                self.load_unlock_secret()
        return self._unlock_secret if self._unlock_secret_ok else None

    def _read_unlock_window(self):
        try:
            with open(self.unlock_path, 'r') as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        mode = data.get('mode')
        if mode not in ('ro', 'rw'):
            return None
        try:
            expires_at = float(data.get('expires_at'))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(expires_at):
            # Infinity/NaN (json accepts both) would be a permanent window.
            return None
        if self._now() >= expires_at:
            return None
        return {'mode': mode, 'expires_at': expires_at}

    def _write_unlock_window(self, window):
        tmp = self.unlock_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump({
                'mode': window['mode'],
                'expires_at': window['expires_at'],
            }, fh, indent=2)
            fh.write('\n')
        os.replace(tmp, self.unlock_path)

    def _open_window(self, mode: str, seconds) -> None:
        window = {
            'mode': mode,
            'expires_at': self._now() + float(seconds),
        }
        self._write_unlock_window(window)
        self._unlock_window = window

    def _close_window(self) -> None:
        self._unlock_window = None
        try:
            os.remove(self.unlock_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning('paa unlock: cannot remove %s',
                        self.unlock_path, exc_info=True)
        # Stale keyboards must not be tappable after a later re-unlock.
        for group in list(self.choices.live_groups()):
            self.choices.expire_group(group)

    def _is_locked(self) -> bool:
        window = self._unlock_window
        if window is not None and self._now() < window['expires_at']:
            return False
        if window is not None:
            # First observation of an expired window: persist the locked
            # state and expire pending keyboards.
            self._close_window()
        return True

    def _rw_window_active(self) -> bool:
        window = self._unlock_window
        return (window is not None
                and window['mode'] == 'rw'
                and self._now() < window['expires_at'])

    def _approval_sock(self):
        """Validate the broker channel for a rw turn.

        The provider contract is ``(sock_path, token)`` or None; the
        token is the per-turn second half of the approval channel and
        must be non-empty. The path half is validated as before
        (exists → S_ISSOCK → connect probe) so a rw turn never runs
        against a path the broker is not actually listening on. Returns
        ``(sock_path, token)`` or None.
        """
        provider = self.approval_sock_provider
        if provider is None:
            return None
        try:
            channel = provider()
        except Exception:  # noqa: BLE001 — a broken broker fails closed
            log.exception('paa unlock: approval socket provider failed')
            return None
        if not channel:
            return None
        try:
            path, token = channel
        except (TypeError, ValueError):
            return None
        path = str(path or '')
        if not path or not str(token or ''):
            return None
        try:
            info = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISSOCK(info.st_mode):
            return None
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(2.0)
                probe.connect(path)
            finally:
                probe.close()
        except OSError:
            # ECONNREFUSED and friends: the socket file exists but nobody
            # is accepting on it.
            return None
        return (path, str(token))

    def _try_unlock(self, user_id, arg):
        """Open a window on success; return ``(mode, seconds)`` or None.

        Every failure — malformed grammar, throttle cooldown, unreadable
        secret, wrong code — returns None so the caller answers one
        identical opaque reply. Failed attempts count toward the throttle,
        except attempts made during an active cooldown, which fail without
        recording (otherwise each attempt would slide the window closed
        again).
        """
        if not self._throttle.allow(user_id):
            return None
        parsed = parse_unlock_args(arg)
        if parsed is None:
            self._throttle.record_failure(user_id)
            return None
        code, mode, seconds = parsed
        secret = self._get_unlock_secret()
        if secret is None:
            self._throttle.record_failure(user_id)
            return None
        if not verify(secret, code, now=self._now()):
            self._throttle.record_failure(user_id)
            return None
        self._throttle.record_success(user_id)
        self._open_window(mode, seconds)
        return mode, seconds

    def _cmd_unlock(self, chat_id, user_id, arg) -> list:
        opened = self._try_unlock(user_id, arg)
        if opened is None:
            return [self._say(chat_id, UNLOCK_FAIL_REPLY)]
        mode, seconds = opened
        return [self._say(chat_id, f'Unlocked {mode} for {seconds}s.')]

    def _cmd_lock(self) -> str:
        self._close_window()
        return LOCKED_REPLY

    def _spool_path(self, update_id) -> str:
        return os.path.join(self.spool_dir, f'{int(update_id):016d}.json')

    def _blank_record(self, update) -> dict:
        return {
            'update': update,
            'ready': False,
            'actions': [],
            'sent': 0,
            'follow': None,
        }

    def _read_record(self, update_id) -> dict | None:
        path = self._spool_path(update_id)
        try:
            with open(path, 'r') as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if isinstance(data, dict) and 'update_id' in data:
            # A spool written before actions were stored beside the update.
            return self._blank_record(data)
        if not isinstance(data, dict) or not isinstance(data.get('update'), dict):
            return None
        data.setdefault('ready', False)
        data.setdefault('actions', [])
        data.setdefault('sent', 0)
        data.setdefault('follow', None)
        return data

    def _write_record(self, record: dict):
        update_id = int(record['update']['update_id'])
        path = self._spool_path(update_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(record, fh)
        os.replace(tmp, path)

    @staticmethod
    def _dump_action(action: Outbound) -> dict:
        return {name: getattr(action, name) for name in _OUT_FIELDS}

    @staticmethod
    def _load_action(row: dict) -> Outbound:
        payload = {name: row.get(name) for name in _OUT_FIELDS}
        payload['kind'] = payload.get('kind') or 'message'
        if payload.get('text') is None:
            payload['text'] = ''
        return Outbound(**payload)

    def persist_update(self, update, *, before_ack=None):
        """Write the update, then acknowledge the offset.

        ``before_ack`` runs after the spool file exists and before the offset
        moves, so a test can see that order. An existing spool is left as it
        is: a later prepare must not wipe actions already stored.
        """
        update_id = int(update['update_id'])
        path = self._spool_path(update_id)
        with self._spool_lock:
            if not os.path.exists(path):
                self._write_record(self._blank_record(update))
        if before_ack is not None:
            before_ack()
        nxt = update_id + 1
        if nxt > self.offset:
            self.offset = nxt
            self._write_offset()

    def _unlink_spool(self, update):
        path = self._spool_path(update['update_id'])
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _store_ready(self, update, actions: list):
        with self._spool_lock:
            record = self._read_record(update['update_id']) or self._blank_record(update)
            record['update'] = update
            record['actions'] = [
                self._dump_action(action) for action in actions
                if action.kind != 'follow'
            ]
            record['follow'] = None
            record['ready'] = True
            self._write_record(record)

    def iter_spooled_updates(self) -> list:
        names = sorted(
            n for n in os.listdir(self.spool_dir) if n.endswith('.json'))
        updates = []
        for name in names:
            path = os.path.join(self.spool_dir, name)
            with open(path, 'r') as fh:
                data = json.load(fh)
            if isinstance(data, dict) and 'update_id' in data:
                updates.append(data)
            elif isinstance(data, dict) and isinstance(data.get('update'), dict):
                updates.append(data['update'])
        return updates

    def ensure_ready(self, update):
        """Finish a spooled update without deleting it.

        A stored reply is not computed again. A stored follow runs once and
        is then remembered, so a failed send retries the send only.
        """
        with self._spool_lock:
            record = self._read_record(update['update_id'])
            ready = bool(record and record.get('ready'))
            follow_row = None if ready or record is None else record.get('follow')
        if ready:
            return
        if follow_row:
            extra = self.run_follow(self._load_action(follow_row))
            with self._spool_lock:
                record = self._read_record(update['update_id']) or self._blank_record(update)
                record['actions'] = list(record.get('actions') or [])
                record['actions'].extend(
                    self._dump_action(action) for action in extra
                    if action.kind != 'follow'
                )
                record['follow'] = None
                record['ready'] = True
                self._write_record(record)
            return
        outs = self._dispatch_taking_worker(update, defer_chat=False)
        self._store_ready(update, outs)

    def unsent_actions(self, update) -> list:
        with self._spool_lock:
            record = self._read_record(update['update_id'])
            if not record:
                return []
            sent = int(record.get('sent') or 0)
            rows = record.get('actions') or []
            return [self._load_action(row) for row in rows[sent:]]

    def mark_sent(self, update):
        with self._spool_lock:
            record = self._read_record(update['update_id'])
            if not record:
                return
            record['sent'] = int(record.get('sent') or 0) + 1
            self._write_record(record)

    def append_ready(self, update, actions: list):
        """Remember follow-up replies and mark the spool ready to send."""
        with self._spool_lock:
            record = self._read_record(update['update_id']) or self._blank_record(update)
            record['actions'] = list(record.get('actions') or [])
            record['actions'].extend(
                self._dump_action(action) for action in actions
                if action.kind != 'follow'
            )
            record['follow'] = None
            record['ready'] = True
            self._write_record(record)

    def commit_delivery(self, update):
        """Drop the spool once every stored action has been sent."""
        with self._spool_lock:
            record = self._read_record(update['update_id'])
            if not record or not record.get('ready'):
                return
            actions = record.get('actions') or []
            if int(record.get('sent') or 0) < len(actions):
                return
            path = self._spool_path(update['update_id'])
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def replay_spool(self) -> list:
        """Finish spooled updates and return their replies.

        Does not delete the spool and does not move the offset. The caller
        deletes a file with :meth:`commit_delivery` after the reply is sent.
        A second call returns the stored reply and does not run the harness
        again.
        """
        outs = []
        for update in self.iter_spooled_updates():
            self.ensure_ready(update)
            with self._spool_lock:
                record = self._read_record(update['update_id']) or {}
                rows = record.get('actions') or []
            outs.extend(self._load_action(row) for row in rows)
        return outs

    # -- updates -----------------------------------------------------------

    def handle_update(self, update, *, before_ack=None) -> list:
        """Run one update to completion, including any chat turn.

        The spool stays on disk until :meth:`commit_delivery`. The Telegram
        loop uses :meth:`prepare_update` so a chat turn does not block polling.
        """
        self.persist_update(update, before_ack=before_ack)
        outs = self._dispatch_taking_worker(update, defer_chat=False)
        self._store_ready(update, outs)
        return outs

    def prepare_update(self, update, *, before_ack=None):
        """Persist and return ``(immediate_actions, follow_or_none)``.

        Immediate actions include ``answerCallbackQuery``. A follow is the
        chat turn, which the caller runs after those actions are sent.
        """
        self.persist_update(update, before_ack=before_ack)
        with self._spool_lock:
            record = self._read_record(update['update_id'])
            if record and record.get('ready'):
                rows = record.get('actions') or []
                return [self._load_action(row) for row in rows], None
        outs = self._dispatch(update, defer_chat=True)
        follow = next((action for action in outs if action.kind == 'follow'), None)
        immediate = [action for action in outs if action.kind != 'follow']
        with self._spool_lock:
            record = self._read_record(update['update_id']) or self._blank_record(update)
            record['update'] = update
            record['actions'] = [self._dump_action(action) for action in immediate]
            record['follow'] = self._dump_action(follow) if follow is not None else None
            record['ready'] = follow is None
            self._write_record(record)
        return immediate, follow

    def publish_inflight_handle(self):
        """Publish a kill handle before the chat thread enters ``chat_turn``.

        ``/stop`` on the poll thread can then kill a turn that has been
        accepted but has not attached its child yet. A second turn does not
        replace the handle the first turn is using.
        """
        if self._handle is None:
            self._handle = HeadlessHandle()
            self._running = True

    def _dispatch_taking_worker(self, update, *, defer_chat: bool) -> list:
        name, _arg = self._command_of(update)
        # /stop must interrupt a running child, so it does not wait on the
        # worker that is blocked inside chat_turn.
        if name == 'stop':
            return self._dispatch(update, defer_chat=defer_chat)
        if self._takes_worker(update, name):
            with self._worker:
                return self._dispatch(update, defer_chat=defer_chat)
        return self._dispatch(update, defer_chat=defer_chat)

    def _takes_worker(self, update, name) -> bool:
        if update.get('callback_query'):
            return False
        if name in (None, 'discuss'):
            # Plain text and /discuss run a chat turn.
            msg = update.get('message') or {}
            if not isinstance(msg.get('text'), str):
                return False
            return True
        return False

    def _dispatch(self, update, *, defer_chat: bool = False) -> list:
        if update.get('callback_query'):
            # An empty allowlist processes the update and replies to nobody.
            if not self._allowlist():
                cq = update.get('callback_query') or {}
                msg = cq.get('message') or {}
                chat = msg.get('chat') or {}
                self._log_operator(
                    (cq.get('from') or {}).get('id'),
                    chat.get('id'),
                    msg.get('date'),
                )
                return []
            if self._is_locked():
                # Answer so the client's spinner stops, but never resolve
                # the choice while locked.
                cq = update.get('callback_query') or {}
                return [Outbound(
                    kind='answer_callback',
                    callback_query_id=cq.get('id'),
                    answer_text=LOCKED_REPLY,
                )]
            return self._on_callback(update, defer_chat=defer_chat)
        msg = update.get('message')
        if not isinstance(msg, dict):
            return []
        chat = msg.get('chat') or {}
        if chat.get('type') != 'private':
            return []
        user_id = (msg.get('from') or {}).get('id')
        chat_id = chat.get('id')
        if not self._allowlist():
            self._log_operator(user_id, chat_id, msg.get('date'))
            return []
        if not self._allowed(user_id):
            return []
        text = msg.get('text')
        is_text = isinstance(text, str) and bool(text.strip())
        name, arg = parse_command(text.strip()) if is_text else (None, None)
        if self._is_locked() and name != 'unlock':
            # The unlock window gates all inbound communication; /unlock
            # is the only live command while locked.
            return [self._say(chat_id, LOCKED_REPLY)]
        if not is_text:
            return [self._say(chat_id, 'Text only.')]
        if name == 'new':
            return [self._say(chat_id, self._cmd_new())]
        if name == 'stop':
            return [self._say(chat_id, self._cmd_stop())]
        if name == 'unlock':
            return self._cmd_unlock(chat_id, user_id, arg)
        if name == 'lock':
            return [self._say(chat_id, self._cmd_lock())]
        if name == 'status':
            return [self._say(chat_id, self._cmd_status())]
        if name == 'help':
            return [self._say(chat_id, self._cmd_help())]
        if name == 'findings':
            return self._cmd_findings(chat_id, user_id, page=0)
        if name == 'ack':
            return [self._say(chat_id, self._cmd_status_change(arg, 'acknowledged'))]
        if name == 'dismiss':
            return [self._say(chat_id, self._cmd_status_change(arg, 'dismissed'))]
        if name == 'discuss':
            if defer_chat:
                return [self._follow('discuss', chat_id, user_id, arg)]
            return self._discuss_messages(chat_id, user_id, arg)
        if defer_chat:
            return [self._follow('prompt', chat_id, user_id, text)]
        return self._prompt_messages(chat_id, user_id, text)

    def _follow(self, kind, chat_id, user_id, prompt, *, policy=None) -> Outbound:
        return Outbound(
            kind='follow', follow=kind, follow_prompt=prompt,
            follow_policy=policy, chat_id=chat_id, user_id=user_id,
        )

    def run_follow(self, action: Outbound) -> list:
        """Run the chat half of an update. The caller already sent the answer.

        The follow was accepted at dispatch time; the lock is re-checked
        here at run time because a deferred or spool-replayed follow can
        start after a lock transition (window expiry or /lock). The check
        happens inside the worker hold so it is atomic with turn start —
        a follow that passed the check while queued cannot slip past an
        expiry that lands while it waits for the worker. When locked, the
        turn is dropped — never run, and never left lingering to fire on
        a later unlock — and the chat gets the locked reply.
        """
        if action.follow not in ('prompt', 'discuss'):
            return []
        with self._worker:
            if self._is_locked():
                return [self._say(action.chat_id, LOCKED_REPLY)]
            if action.follow == 'prompt':
                return self._prompt_messages(
                    action.chat_id, action.user_id, action.follow_prompt or '',
                    policy=action.follow_policy,
                )
            return self._discuss_messages(
                action.chat_id, action.user_id, action.follow_prompt or '',
            )

    def _command_of(self, update):
        msg = update.get('message') or {}
        text = msg.get('text')
        if not isinstance(text, str):
            return None, None
        return parse_command(text.strip())

    def _allowlist(self) -> list:
        return list(self.config.get('allowed_user_ids') or [])

    def _allowed(self, user_id) -> bool:
        try:
            return int(user_id) in self._allowlist()
        except (TypeError, ValueError):
            return False

    def _log_operator(self, user_id, chat_id, date):
        line = f'user={user_id} chat={chat_id} date={date}\n'
        with open(self.operator_log, 'a') as fh:
            fh.write(line)

    def _say(self, chat_id, text, *, markup=None, group=None, message_id=None,
             kind='message') -> Outbound:
        if kind == 'message' and message_id is None:
            self._next_mid += 1
            message_id = self._next_mid
        return Outbound(
            kind=kind, chat_id=chat_id, text=text, reply_markup=markup,
            message_id=message_id, choice_group=group,
        )

    # -- commands ----------------------------------------------------------

    def _cmd_new(self) -> str:
        with self._state_lock:
            if self.session_id:
                self._archive.append(self.session_id)
                self._archive = self._archive[-10:]
            self.session_id = None
            self._gen += 1
            self._save_session()
        return 'Session cleared. This applies to the next turn.'

    def _cmd_stop(self) -> str:
        handle = self._handle
        running = self._running
        if handle is not None:
            handle.kill()
        if handle is not None or running:
            return 'Stopped.'
        return 'No turn is running.'

    def _cmd_status(self) -> str:
        hid = self.harness_id or self.settings.effective_harness('')
        session = 'yes' if self.session_id else 'no'
        running = 'yes' if self._running else 'no'
        lines = [
            f'Harness: {hid}',
            f'Session: {session}',
            f'Turn running: {running}',
        ]
        window = self._unlock_window
        if window is not None and self._now() < window['expires_at']:
            remaining = int(window['expires_at'] - self._now())
            lines.append('Lock: unlocked')
            lines.append(f"Mode: {window['mode']}")
            lines.append(f'Seconds remaining: {remaining}')
        else:
            lines.append('Lock: locked')
        lines.append('MCP review: up' if self._review_channel_up()
                     else 'MCP review: down')
        broker = getattr(self, 'approval_broker', None)
        if broker is not None:
            lines.append(f'Reviewer: {broker.last_verdict or "—"}')
        return '\n'.join(lines)

    def _review_channel_up(self) -> bool:
        """The /status liveness contract: the broker socket accepting AND
        this tree's paa_mcp.py present. Both halves are required before a
        rw turn is allowed to run (serve() applies the same check)."""
        broker = getattr(self, 'approval_broker', None)
        if broker is not None:
            up = broker.alive()
        else:
            up = bool(self._approval_sock())
        if not up:
            return False
        try:
            from paa_mcp import server_file_path
            return os.path.isfile(server_file_path())
        except Exception:  # noqa: BLE001 — status must never raise
            return False

    def _cmd_help(self) -> str:
        return (
            'Commands:\n'
            '/new — clear the session (applies to the next turn)\n'
            '/stop — stop the running turn\n'
            '/unlock <code> [ro|rw] [seconds] — unlock with your authenticator code\n'
            '/lock — lock immediately\n'
            '/status — harness, session, running, lock\n'
            '/findings — list pending findings\n'
            '/ack ID — acknowledge a finding\n'
            '/dismiss ID — dismiss a finding\n'
            '/discuss ID — discuss a finding\n'
            '/help — this message\n'
            'Any other text is a message to the PAA.'
        )

    # -- chat --------------------------------------------------------------

    def _run_prompt(self, prompt, *, policy=None, chat_id=None,
                    user_id=None) -> str:
        """Run one chat turn. Called with the worker held (run_follow).

        Gate turns bind the approval broker ATOMICALLY with the chat
        turn: ``begin_turn`` runs here — under the worker, after the
        lock check, once the policy is final — so only the actually
        running turn can hold the approval channel. A queued follow
        cannot mint a token that clobbers the running turn, and no
        window exists between worker release and ``end_turn``.
        """
        approval_sock = approval_token = None
        gate_turn = False
        broker = self.approval_broker
        # tool_policy='armed' is deliberately unselectable by the bot
        # (the maintainer 2026-10-01: mutations require OTP elevation to rw plus
        # the MCP reviewer; the unreviewed /arm path is gone). A stale
        # request for it — e.g. a pre-upgrade spool record — fails
        # closed to the read-only 'plan' policy.
        if policy == 'armed':
            log.warning('paa turns: armed turn requested; running plan instead')
            policy = 'plan'
        with self._state_lock:
            if policy is None and self._rw_window_active():
                # rw window: mutating tools go through the approval broker.
                policy = 'gate'
            if policy is None:
                policy = 'plan'
            gen = self._gen
            session_id = self.session_id
            self.harness_id = self.settings.effective_harness('')
        if policy == 'gate':
            # Begin BEFORE validating the channel: the broker only hands
            # out (sock_path, token) for the active turn. A refused turn
            # ends its approval turn on the spot (fail closed, no leak).
            if broker is not None:
                broker.begin_turn(chat_id, user_id, prompt)
                gate_turn = True
            channel = self._approval_sock()
            if not channel:
                if gate_turn:
                    broker.end_turn()
                return RW_REFUSAL
            approval_sock, approval_token = channel
        # Reuse the handle published by publish_inflight_handle so /stop,
        # which may already have killed it, still applies to this turn.
        handle = self._handle or HeadlessHandle()
        self._handle = handle
        self._running = True
        try:
            result = self.chat_turn(
                self.settings,
                prompt,
                session_id=session_id,
                project_path='',
                tool_policy=policy,
                timeout=int(self.config.get('chat_timeout_sec') or CHAT_TIMEOUT_SEC),
                handle=handle,
                approval_sock=approval_sock,
                approval_token=approval_token,
                reasoning_effort=self.config.get('chat_effort'),
            )
        finally:
            self._running = False
            if self._handle is handle:
                self._handle = None
            if gate_turn:
                try:
                    broker.end_turn()
                except Exception:  # noqa: BLE001 — never mask the turn
                    log.exception('paa approval: end_turn failed')
        with self._state_lock:
            if gen == self._gen and getattr(result, 'session_id', None):
                self.session_id = result.session_id
                self._save_session()
            elif gen == self._gen:
                self._save_session()
        return format_turn_reply(result)

    def _prompt_messages(self, chat_id, user_id, prompt, *, policy=None) -> list:
        text = self._run_prompt(
            prompt, policy=policy, chat_id=chat_id, user_id=user_id)
        return [self._say(chat_id, text)]

    # -- callbacks ---------------------------------------------------------

    def _on_callback(self, update, *, defer_chat: bool = False) -> list:
        cq = update.get('callback_query') or {}
        user_id = (cq.get('from') or {}).get('id')
        data = cq.get('data')
        cq_id = cq.get('id')
        # Allowlist is checked here, before any harness call.
        result = self.choices.resolve(
            data, user_id=user_id, allowlist=self._allowlist())
        outs = [Outbound(
            kind='answer_callback',
            callback_query_id=cq_id,
            answer_text=result.answer_text or None,
        )]
        if result.kind in ('unknown', 'foreign', 'spent', 'expired'):
            return outs
        chat_id = result.chat_id
        message_id = result.message_id
        if result.kind in ('page', 'drill'):
            text = result.prompt or ''
            meaning = result.meaning or {}
            if result.kind == 'page' and meaning.get('kind') == 'findings-open':
                text = findings_page_text(
                    self._pending(), int(result.value or 0))
            outs.append(self._say(
                chat_id, text,
                markup=result.reply_markup, group=result.group,
                message_id=message_id, kind='edit',
            ))
            return outs
        # Selected. State the choice and drop the keyboard.
        statement = result.statement or result.label or 'Chosen'
        shown = statement
        if result.prompt:
            shown = f'{result.prompt} — {statement}'
        outs.append(self._say(
            chat_id, shown,
            markup={'inline_keyboard': []},
            message_id=message_id, kind='edit',
        ))
        meaning = result.meaning or {}
        kind = meaning.get('kind')
        if kind == 'finding':
            ledger_id = meaning.get('ledger_id')
            if result.value == 'ack':
                self._set_status(ledger_id, 'acknowledged')
            elif result.value == 'dismiss':
                self._set_status(ledger_id, 'dismissed')
            elif result.value == 'discuss' and ledger_id:
                if defer_chat:
                    outs.append(self._follow(
                        'discuss', chat_id, user_id, str(ledger_id)))
                else:
                    with self._worker:
                        outs.extend(self._discuss_messages(
                            chat_id, user_id, str(ledger_id)))
            return outs
        if kind == 'findings-open' and result.value:
            if defer_chat:
                outs.append(self._follow(
                    'discuss', chat_id, user_id, str(result.value)))
            else:
                with self._worker:
                    outs.extend(self._discuss_messages(
                        chat_id, user_id, str(result.value)))
            return outs
        if kind == 'approval':
            # The broker thread's waiter observes the outcome via
            # ChoiceBook.resolve settling the group; the generic selected
            # edit above already states the choice and drops the keyboard.
            return outs
        return outs

    # -- findings ----------------------------------------------------------

    def _set_status(self, item_id, status) -> bool:
        if self.ledger is None or not item_id:
            return False
        if item_id not in getattr(self.ledger, '_items', {}) and hasattr(self.ledger, 'load'):
            # update_status reloads under the lock; a miss before that is fine.
            pass
        changed = self.ledger.update_status(item_id, status)
        return bool(changed)

    def _cmd_status_change(self, item_id, status) -> str:
        item_id = (item_id or '').strip()
        if not item_id:
            verb = 'ack' if status == 'acknowledged' else 'dismiss'
            return f'Usage: /{verb} <id>'
        if self.ledger is None:
            return 'No ledger.'
        if not self._set_status(item_id, status):
            # Reload so a concurrent add is visible, then report.
            self.ledger.load()
            if item_id not in self.ledger._items:
                return f'No finding {item_id}.'
        word = 'Acknowledged' if status == 'acknowledged' else 'Dismissed'
        return f'{word} {item_id}.'

    def _cmd_findings(self, chat_id, user_id, page: int) -> list:
        items = self._pending()
        text = findings_page_text(items, page, PAGE_SIZE)
        options = []
        # Page buttons only — the list itself is the message text.
        # A drill-capable placeholder keeps long lists on one keyboard helper.
        if len(items) > PAGE_SIZE:
            # Represent pages as options so the shared helper pages at 8.
            # Each "option" is one finding line; the helper slices to 8 and
            # adds Back/Next. Selecting a row discusses that finding.
            options = [
                {'label': finding_notice_text(item), 'value': item.id}
                for item in items
            ]
            meaning = {'kind': 'findings-open'}
            group, prompt, markup = self.choices.create(
                chat_id=chat_id, user_id=user_id, prompt=text,
                options=options, meaning=meaning, page=page,
            )
            self._findings_group = group
            msg = self._say(chat_id, text, markup=markup, group=group)
            self.choices.bind_message(group, msg.message_id)
            return [msg]
        return [self._say(chat_id, text)]

    def _discuss_messages(self, chat_id, user_id, item_id) -> list:
        item_id = (item_id or '').strip()
        if not item_id:
            return [self._say(chat_id, 'Usage: /discuss <id>')]
        if self.ledger is None:
            return [self._say(chat_id, 'No ledger.')]
        self.ledger.load()
        item = self.ledger._items.get(item_id)
        if item is None:
            return [self._say(chat_id, f'No finding {item_id}.')]
        prompt = discuss_finding_prompt(item, self.ledger.pending_items())
        return self._prompt_messages(chat_id, user_id, prompt)

    def _pending(self):
        if self.ledger is None:
            return []
        self.ledger.load()
        return self.ledger.pending_items()

    def notify_new_findings(self, before_ids, *, chat_id, user_id) -> list:
        """One short message per new pending id, with Ack / Dismiss / Discuss."""
        if self.ledger is None:
            return []
        outs = []
        for item in self.ledger.pending_items():
            if item.id in before_ids:
                continue
            text = finding_notice_text(item)
            options = [
                {'label': 'Ack', 'value': 'ack'},
                {'label': 'Dismiss', 'value': 'dismiss'},
                {'label': 'Discuss', 'value': 'discuss'},
            ]
            group, _prompt, markup = self.choices.create(
                chat_id=chat_id, user_id=user_id, prompt=text,
                options=options,
                meaning={'kind': 'finding', 'ledger_id': item.id},
            )
            msg = self._say(chat_id, text, markup=markup, group=group)
            self.choices.bind_message(group, msg.message_id)
            outs.append(msg)
        return outs

    def maybe_scan(self, monitor, *, now, chat_id=None, user_id=None) -> list:
        """Run the shared scan on ``paa_loop_interval_minutes`` when PAA is on.

        ``monitor.run_scan`` holds the scan lock. A lock miss returns without
        a second scan, and this method then has no new rows to announce.
        Finding notices are off unless ``findings_notices`` is set in the
        bot config — automated findings do not go to Telegram by default.
        """
        if not getattr(self.settings, 'paa_enabled', False):
            return []
        interval = max(1, int(self.settings.paa_loop_interval_minutes)) * 60
        if self._last_scan and now - self._last_scan < interval:
            return []
        before = set()
        if self.ledger is not None:
            before = {i.id for i in self.ledger.pending_items()}
        monitor.run_scan()
        self._last_scan = now
        if (chat_id is None or user_id is None or self.ledger is None
                or not self.config.get('findings_notices')):
            return []
        return self.notify_new_findings(before, chat_id=chat_id, user_id=user_id)
