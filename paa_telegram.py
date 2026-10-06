"""Telegram long-poll transport for the PAA turn owner.

Stdlib ``urllib`` only. No Markdown. Token comes from ``pass show`` of the
configured entry (default ``internal/telegram/paa-bot-token``) and is never
written to config, the unit, or the log.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

from paa_turns import (
    TurnService,
    default_state_dir,
    load_bot_config,
)

log = logging.getLogger('paa_telegram')

API_ROOT = 'https://api.telegram.org'
LONG_POLL_SEC = 50
SPLIT_AT = 4000
N8N_ENTRY = 'internal/telegram/n8n-bot-token'
# Telegram clears the typing indicator ~5s after the last sendChatAction.
TYPING_INTERVAL_SEC = 4.0


def split_outbound(text: str, limit: int = SPLIT_AT) -> list:
    """Split ``text`` only once it passes ``limit``. Parts concatenate back."""
    if text is None:
        text = ''
    if len(text) <= limit:
        return [text]
    parts = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind('\n', 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


class TelegramApi:
    """One bot token against one API root (production or a loopback stand-in)."""

    def __init__(self, base_url, token, *, reload_token=None,
                 long_poll_sec=LONG_POLL_SEC):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.reload_token = reload_token
        self.long_poll_sec = long_poll_sec
        self._retrying = False

    def _url(self, method: str) -> str:
        return f'{self.base_url}/bot{self.token}/{method}'

    def call(self, method: str, payload: dict, *, http_timeout: int = 70,
             quiet: bool = False) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(method),
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                body = json.loads(resp.read().decode() or '{}')
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self.reload_token and not self._retrying:
                self._retrying = True
                try:
                    self.token = self.reload_token()
                    return self.call(method, payload, http_timeout=http_timeout,
                                     quiet=quiet)
                finally:
                    self._retrying = False
            # Do not log the request URL; it contains the token.
            if not quiet:
                log.fatal('telegram %s failed: HTTP %s', method, exc.code)
            raise
        except urllib.error.URLError as exc:
            if not quiet:
                log.fatal('telegram %s failed: %s', method, exc.reason)
            raise
        if not isinstance(body, dict):
            raise RuntimeError(f'telegram {method} returned a non-object')
        if body.get('ok') is False:
            raise RuntimeError(body.get('description') or f'telegram {method} not ok')
        return body

    def send_chat_action(self, chat_id, action: str = 'typing') -> bool:
        """Best-effort indicator refresh. Failure is debug-level, never raised."""
        try:
            self.call('sendChatAction', {'chat_id': chat_id, 'action': action},
                      quiet=True)
            return True
        except Exception:
            log.debug('telegram sendChatAction failed', exc_info=True)
            return False

    def get_updates(self, offset: int, timeout: int | None = None) -> list:
        if timeout is None:
            timeout = self.long_poll_sec
        body = self.call('getUpdates', {
            'offset': offset,
            'timeout': timeout,
            'allowed_updates': ['message', 'callback_query'],
        }, http_timeout=timeout + 20)
        result = body.get('result') or []
        return result if isinstance(result, list) else []

    def deliver(self, action) -> dict | None:
        if action.kind == 'message':
            parts = split_outbound(action.text)
            last = None
            for index, part in enumerate(parts):
                payload = {'chat_id': action.chat_id, 'text': part}
                if index == len(parts) - 1 and action.reply_markup is not None:
                    payload['reply_markup'] = action.reply_markup
                last = self.call('sendMessage', payload)
            return last
        if action.kind == 'edit':
            payload = {
                'chat_id': action.chat_id,
                'message_id': action.message_id,
                'text': action.text if action.text else '.',
            }
            if action.reply_markup is not None:
                payload['reply_markup'] = action.reply_markup
            return self.call('editMessageText', payload)
        if action.kind == 'answer_callback':
            payload = {'callback_query_id': action.callback_query_id}
            if action.answer_text:
                payload['text'] = action.answer_text
            return self.call('answerCallbackQuery', payload)
        return None


class TypingSender:
    """Re-send ``sendChatAction`` 'typing' until :meth:`stop`.

    Telegram clears the indicator about five seconds after the last
    action, so the first send is immediate and the rest repeat on
    ``interval``. Every send is best-effort: the API call swallows its
    own failures, so this thread never raises. The thread is a daemon
    and exits on :meth:`stop`, which joins it; a send already in flight
    at stop time still finishes.
    """

    def __init__(self, api, chat_id, *, interval=TYPING_INTERVAL_SEC):
        self.api = api
        self.chat_id = chat_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name='paa-typing', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            self.api.send_chat_action(self.chat_id, 'typing')
            self._stop.wait(self.interval)


class TelegramLoop:
    """Poll, send immediate replies, and run chat turns off the poll thread.

    ``/stop`` is read by the next ``getUpdates`` while a turn is still
    running. ``answerCallbackQuery`` is sent before that turn's ``chat_turn``.
    A spool file stays until every outbound action for it has been sent.
    A follow (chat turn) refreshes Telegram's typing indicator from its own
    daemon thread until the turn's reply has been delivered.
    """

    def __init__(self, api: TelegramApi, service: TurnService,
                 *, typing_interval=TYPING_INTERVAL_SEC):
        self.api = api
        self.service = service
        self.typing_interval = typing_interval
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def wait_idle(self, timeout: float = 5) -> bool:
        return self._idle.wait(timeout)

    def _bind(self, action, result):
        if (action.kind == 'message' and action.choice_group
                and isinstance(result, dict)):
            mid = (result.get('result') or {}).get('message_id')
            if mid is not None:
                self.service.choices.bind_message(action.choice_group, mid)

    def deliver_outbound(self, actions) -> list:
        """Send replies that are not a polled update, and keep choice ids.

        Scan notices are built with a local stand-in message id. The id
        Telegram assigns on ``sendMessage`` replaces it, so a later
        Ack, Dismiss, or Discuss edits that message.
        """
        sent = []
        for action in actions:
            if getattr(action, 'kind', None) == 'follow':
                continue
            result = self.api.deliver(action)
            sent.append(result)
            self._bind(action, result)
        return sent

    def _deliver_unsent(self, update) -> list:
        sent = []
        while True:
            pending = self.service.unsent_actions(update)
            if not pending:
                break
            action = pending[0]
            if action.kind == 'follow':
                break
            result = self.api.deliver(action)
            sent.append(result)
            self._bind(action, result)
            self.service.mark_sent(update)
        return sent

    def deliver_spool(self):
        """Send replies left from a crash. A failed send leaves the spool."""
        for update in self.service.iter_spooled_updates():
            try:
                self.service.ensure_ready(update)
                self._deliver_unsent(update)
                self.service.commit_delivery(update)
            except Exception:
                log.exception(
                    'telegram spool delivery failed for update %s',
                    update.get('update_id'),
                )
                break

    def _start_follow(self, update, follow):
        with self._inflight_lock:
            self._inflight += 1
            self._idle.clear()
        threading.Thread(
            target=self._follow_body, args=(update, follow), daemon=True,
        ).start()

    def _follow_body(self, update, follow):
        typing = None
        if follow.chat_id is not None:
            typing = TypingSender(
                self.api, follow.chat_id, interval=self.typing_interval)
        try:
            if typing is not None:
                typing.start()
            extra = self.service.run_follow(follow)
            self.service.append_ready(update, extra)
            self._deliver_unsent(update)
            self.service.commit_delivery(update)
        except Exception:
            log.exception(
                'telegram follow failed for update %s',
                update.get('update_id'),
            )
        finally:
            if typing is not None:
                typing.stop()
            with self._inflight_lock:
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()

    def poll_once(self, timeout: int | None = None) -> list:
        updates = self.api.get_updates(self.service.offset, timeout=timeout)
        sent = []
        for update in updates:
            _immediate, follow = self.service.prepare_update(update)
            try:
                sent.extend(self._deliver_unsent(update))
            except Exception:
                # The spool still holds every unsent action.
                raise
            if follow is not None:
                self.service.publish_inflight_handle()
                self._start_follow(update, follow)
            else:
                self.service.commit_delivery(update)
        return sent


def read_pass_token(entry: str) -> str:
    """``pass show``. Failure is fatal. The n8n entry is never requested."""
    if not isinstance(entry, str) or not entry.strip():
        log.fatal('fatal: pass entry is empty')
        raise SystemExit(1)
    entry = entry.strip()
    if entry == N8N_ENTRY or entry.endswith('/n8n-bot-token'):
        log.fatal('fatal: refusing to read the n8n telegram token')
        raise SystemExit(1)
    try:
        proc = subprocess.run(
            ['pass', 'show', entry],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        log.fatal('fatal: pass invocation failed: %s', exc)
        raise SystemExit(1) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or 'pass failed').strip()
        log.fatal('fatal: pass show failed: %s', err)
        raise SystemExit(1)
    lines = [ln.strip() for ln in (proc.stdout or '').splitlines() if ln.strip()]
    if not lines:
        log.fatal('fatal: pass show returned an empty secret')
        raise SystemExit(1)
    return lines[0]


def build_service(state_dir, settings, chat_turn_fn, ledger=None) -> TurnService:
    return TurnService(
        state_dir, settings, chat_turn_fn, ledger=ledger)


def publish_scan(loop, service, monitor, *, now, chat_id):
    """One scan tick. Each new finding notice is bound to Telegram's message id."""
    if monitor is None or chat_id is None:
        return []
    outs = service.maybe_scan(
        monitor, now=now, chat_id=chat_id, user_id=chat_id)
    return loop.deliver_outbound(outs)


def run_tick(loop, service, monitor, *, now, chat_id, timeout):
    """One iteration of :func:`serve`: poll, then publish scan notices."""
    loop.poll_once(timeout)
    return publish_scan(loop, service, monitor, now=now, chat_id=chat_id)


def serve(state_dir: str, token: str, *, base_url: str = API_ROOT) -> None:
    """Block on long-poll. Imports the GTK monitor only after the token loads."""
    from settings import Settings
    from paa_ledger import Ledger
    from paa_headless import chat_turn, default_paa_cwd, sync_paa_persona
    from paa_monitor import PAAMonitor
    from model import ProjectStore
    from paa_approve import ApprovalBroker, remove_hook_config
    from paa_mcp import (
        ensure_mcp_config, ensure_trusted_folder, server_file_path,
    )

    settings = Settings.load()
    ledger = Ledger()
    ledger.load()
    service = TurnService(state_dir, settings, chat_turn, ledger=ledger)
    # Startup read of the TOTP secret for /unlock. Failure is logged by
    # the service; unlock attempts then fail opaquely until a read works.
    service.load_unlock_secret()
    api = TelegramApi(base_url, token, reload_token=lambda: read_pass_token(
        service.config.get('pass_entry') or 'internal/telegram/paa-bot-token'))
    # Retire the dead hook path. Grok never fires PreToolUse hooks in
    # headless -p mode (probe-proven 2026-10-01), so the hook-based gate
    # cannot enforce anything. The file the old self-install wrote is
    # removed only when its content is recognizably ours; foreign files
    # are left alone.
    try:
        hook_path, hook_removed = remove_hook_config()
        if hook_removed:
            log.info('paa approval: retired dead hook config %s', hook_path)
    except Exception:
        log.exception('paa approval: hook config retirement failed')
    # MCP mutation channel: the PAA cwd's .grok/config.toml gains (or
    # refreshes) [mcp_servers.paa-shell] pointing at THIS tree's
    # paa_mcp.py, and the bot-owned cwd is marked trusted in grok's
    # folder-trust store (repo-local MCP config applies only once
    # trusted — verified by live probe of grok 1.0.44, 2026-10-01).
    # Both installs are idempotent content-diff rewrites.
    paa_cwd = default_paa_cwd(settings)
    try:
        cfg_path, cfg_changed = ensure_mcp_config(paa_cwd)
        log.info('paa mcp config %s: %s',
                 'written' if cfg_changed else 'already current', cfg_path)
    except Exception:
        log.exception('paa mcp: config self-install failed')
    try:
        trust_path, trust_changed = ensure_trusted_folder(paa_cwd)
        if trust_changed:
            log.info('paa mcp: marked %s trusted in %s', paa_cwd, trust_path)
    except Exception:
        log.exception('paa mcp: folder trust self-install failed')
    # Persona docs: the deployed AGENTS.md / .system/AGENTS-SUPPLEMENT.md
    # are stale regular-file copies of the repo's paa/ package (verified
    # 2026-10-01), so the Telegram-channel instructions ("The Telegram
    # Channel") never reached the model. Sync them idempotently on startup.
    try:
        for dest_rel, changed in sync_paa_persona(paa_cwd):
            if changed:
                log.info('paa persona: synced %s into %s', dest_rel, paa_cwd)
    except Exception:
        log.exception('paa persona: sync failed')
    broker = ApprovalBroker(state_dir, api, service, chat_turn_fn=chat_turn)
    broker.start()
    # rw turns run only when the review channel is fully up: the broker
    # socket accepting AND this tree's MCP server file present (the
    # /status "MCP review" line reports the same two facts). Otherwise
    # rw is refused — never run un-gated.
    if broker.alive() and os.path.isfile(server_file_path()):
        # (sock_path, per-turn token) or None when no turn is active.
        service.approval_sock_provider = broker.channel
    else:
        log.error('paa approval: review channel incomplete; rw refused')
        service.approval_sock_provider = None
    service.approval_broker = broker
    loop = TelegramLoop(api, service)
    # Crash recovery: send stored replies, then live poll.
    try:
        loop.deliver_spool()
        monitor = None
        if settings.paa_enabled:
            monitor = PAAMonitor(ProjectStore(settings), ledger, settings)
        allow = service.config.get('allowed_user_ids') or []
        chat_id = allow[0] if allow else None
        import time
        while True:
            run_tick(
                loop, service, monitor,
                now=time.time(), chat_id=chat_id, timeout=LONG_POLL_SEC,
            )
    finally:
        broker.stop()


def main(argv=None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    state_dir = default_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    config = load_bot_config(os.path.join(state_dir, 'paa-telegram.json'))
    entry = config.get('pass_entry') or 'internal/telegram/paa-bot-token'
    token = read_pass_token(entry)
    serve(state_dir, token)


if __name__ == '__main__':
    main()
