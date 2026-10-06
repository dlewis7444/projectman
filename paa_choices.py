"""Opaque inline-keyboard choices for the PAA Telegram bot.

Button ``callback_data`` is ``c`` plus 8 url-safe characters (well under
Telegram's 64-byte cap). The option list, prompt, and meaning — including
any ledger id — live in the choice file, never on the button.

Threading: ``ChoiceBook`` takes an ``RLock`` so create/expire/resolve are
safe when a broker thread manages choices while the poll thread resolves
callbacks. One-shot :class:`ChoiceWaiter` handles (registry: ``waiter()``)
let a broker thread block until a group is selected or expires.
"""
from __future__ import annotations

import json
import os
import secrets
import string
import threading
import time

from paa_findings import FINDING_PAGE_SIZE

_ALPHABET = string.ascii_letters + string.digits + '-_'
PAGE_SIZE = FINDING_PAGE_SIZE

_UNSET = object()
_EXPIRED = object()


class ChoiceResult:
    def __init__(self, kind, answer_text, **extra):
        self.kind = kind
        self.answer_text = answer_text
        for key, value in extra.items():
            setattr(self, key, value)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return None


class ChoiceWaiter:
    """One-shot wait handle for one choice group.

    The poll thread settles it from :meth:`ChoiceBook.resolve` (or
    :meth:`ChoiceBook.expire_group`) via the module registry; the broker
    thread blocks in :meth:`wait`:

    * group selected  → returns the chosen option's ``value``,
      ``expired`` stays False
    * group expired   → returns None, ``expired`` True (set as soon as the
      expiry is recorded, before ``wait`` is even called)
    * deadline passes → returns None, ``expired`` False

    A selected option whose ``value`` is itself ``None`` is indistinguishable
    from a timeout by return value alone — give gated options real values.
    """

    def __init__(self, group):
        self.group = group
        self._event = threading.Event()
        self._outcome = _UNSET
        self.expired = False

    @property
    def settled(self) -> bool:
        return self._outcome is not _UNSET

    def wait(self, timeout=None):
        """Block up to *timeout* seconds; see class doc for the contract."""
        if not self._event.wait(timeout):
            return None
        if self._outcome is _EXPIRED:
            self.expired = True
            return None
        return self._outcome

    def _settle(self, outcome) -> None:
        if self.settled:
            return
        self._outcome = outcome
        if outcome is _EXPIRED:
            self.expired = True
        self._event.set()


_WAITERS_LOCK = threading.Lock()
_WAITERS: dict = {}
_OUTCOMES: dict = {}


def waiter(group) -> ChoiceWaiter:
    """Return the wait handle for *group*, creating it if absent.

    A group that already settled (selected or expired) returns an
    already-settled handle, so a late ``waiter(group).wait()`` never blocks
    on a decision that happened before the handle existed. Handles and
    outcomes are kept per group id (unique per create); like the button
    rows, they are never purged.
    """
    with _WAITERS_LOCK:
        recorded = _OUTCOMES.get(group, _UNSET)
        if recorded is not _UNSET:
            handle = ChoiceWaiter(group)
            handle._settle(recorded)
            return handle
        handle = _WAITERS.get(group)
        if handle is None:
            handle = ChoiceWaiter(group)
            _WAITERS[group] = handle
        return handle


def _settle_waiters(group, outcome) -> None:
    """Settle every pending handle for *group*; record the first outcome."""
    with _WAITERS_LOCK:
        if group not in _OUTCOMES:
            _OUTCOMES[group] = outcome
        for handle in _WAITERS.values():
            if handle.group == group:
                handle._settle(outcome)


class ChoiceBook:
    def __init__(self, path, *, now_fn=None, ttl_sec=86400):
        self.path = path
        self._now = now_fn or time.time
        self.ttl_sec = ttl_sec
        self._lock = threading.RLock()
        self._buttons: dict = {}
        self.load()

    def load(self):
        with self._lock:
            self._buttons = {}
            try:
                with open(self.path, 'r') as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError, TypeError):
                return
            buttons = data.get('buttons') if isinstance(data, dict) else None
            if isinstance(buttons, dict):
                self._buttons = buttons

    def save(self):
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'buttons': self._buttons}, fh, indent=2)
                fh.write('\n')
            os.replace(tmp, self.path)

    def _new_id(self) -> str:
        # Caller must hold self._lock.
        for _ in range(50):
            cid = 'c' + ''.join(secrets.choice(_ALPHABET) for _ in range(8))
            if cid not in self._buttons:
                return cid
        raise RuntimeError('choice id space exhausted')

    def create(self, *, chat_id, user_id, prompt, options, meaning,
               message_id=None, page=0):
        """Store ``options`` and return ``(group, text, reply_markup)``.

        Each option is ``{label, value, children?}``. Options with children
        drill in place. More than ``PAGE_SIZE`` options grow page buttons.
        """
        with self._lock:
            group = self._new_id()
            expires = self._now() + self.ttl_sec
            markup = self._install_page(
                group=group,
                chat_id=chat_id,
                user_id=user_id,
                prompt=prompt,
                options=list(options),
                meaning=meaning or {},
                message_id=message_id,
                page=page,
                expires=expires,
            )
            self.save()
            return group, prompt, markup

    def _install_page(self, *, group, chat_id, user_id, prompt, options,
                      meaning, message_id, page, expires):
        # Caller must hold self._lock (RLock: create/resolve re-enter).
        # Drop previous buttons for this group so a replaced keyboard's old
        # ids answer as expired rather than firing a stale action.
        for cid, row in list(self._buttons.items()):
            if row.get('group') == group and not row.get('spent'):
                row['expires'] = 0
        page = max(0, int(page))
        start = page * PAGE_SIZE
        chunk = options[start:start + PAGE_SIZE]
        rows = []
        for opt in chunk:
            cid = self._new_id()
            children = opt.get('children') or []
            action = 'drill' if children else 'select'
            self._buttons[cid] = {
                'group': group,
                'label': opt.get('label') or '',
                'value': opt.get('value'),
                'children': children,
                'action': action,
                'user_id': user_id,
                'chat_id': chat_id,
                'message_id': message_id,
                'expires': expires,
                'spent': False,
                'prompt': prompt,
                'meaning': meaning,
                'options': options,
                'page': page,
            }
            rows.append([{'text': opt.get('label') or '', 'callback_data': cid}])
        nav = []
        if start > 0:
            prev = self._new_id()
            self._buttons[prev] = self._nav_button(
                group, user_id, chat_id, message_id, expires, prompt,
                meaning, options, page - 1, 'Back',
            )
            nav.append({'text': 'Back', 'callback_data': prev})
        if start + PAGE_SIZE < len(options):
            nxt = self._new_id()
            self._buttons[nxt] = self._nav_button(
                group, user_id, chat_id, message_id, expires, prompt,
                meaning, options, page + 1, 'Next',
            )
            nav.append({'text': 'Next', 'callback_data': nxt})
        if nav:
            rows.append(nav)
        return {'inline_keyboard': rows}

    def _nav_button(self, group, user_id, chat_id, message_id, expires,
                    prompt, meaning, options, page, label):
        return {
            'group': group,
            'label': label,
            'value': page,
            'children': [],
            'action': 'page',
            'user_id': user_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'expires': expires,
            'spent': False,
            'prompt': prompt,
            'meaning': meaning,
            'options': options,
            'page': page,
        }

    def bind_message(self, group, message_id):
        with self._lock:
            for row in self._buttons.values():
                if row.get('group') == group:
                    row['message_id'] = message_id
            self.save()

    def live_groups(self):
        """Groups with at least one unspent, unexpired button.

        Lets a caller (e.g. the PAA unlock gate) expire every pending
        keyboard, e.g. when the bot locks and stale buttons must not be
        tappable after a later unlock.
        """
        with self._lock:
            groups = set()
            for row in self._buttons.values():
                if row.get('spent') or row.get('chosen'):
                    continue
                expires = row.get('expires')
                if expires is None or self._now() <= float(expires):
                    groups.add(row.get('group'))
            groups.discard(None)
            return groups

    def expire_group(self, group):
        with self._lock:
            for row in self._buttons.values():
                if row.get('group') == group:
                    row['expires'] = 0
            self.save()
        _settle_waiters(group, _EXPIRED)

    def group_message_id(self, group):
        with self._lock:
            for row in self._buttons.values():
                if row.get('group') == group and row.get('message_id') is not None:
                    return row.get('message_id')
        return None

    def group_outcome(self, group):
        """Decided state from the stored rows, or None when still pending.

        Returns ``('selected', value)`` when a row in *group* is chosen,
        ``('expired', None)`` when every row is past its expiry. Lets a
        caller that never registered a waiter learn the outcome anyway.
        """
        with self._lock:
            saw_row = False
            saw_live = False
            for row in self._buttons.values():
                if row.get('group') != group:
                    continue
                saw_row = True
                if row.get('chosen'):
                    return 'selected', row.get('value')
                expires = row.get('expires')
                if expires is None or self._now() <= float(expires):
                    saw_live = True
        if saw_row and not saw_live:
            return 'expired', None
        return None

    def resolve(self, callback_data, *, user_id, allowlist) -> ChoiceResult:
        """Answer one callback. Unknown, expired, and foreign never select.

        Safe to call from the poll thread while a broker thread creates or
        expires groups. A 'selected' result settles the group's waiters with
        the chosen value; an 'expired' result settles them as expired.
        """
        with self._lock:
            row = self._buttons.get(callback_data)
            if row is None:
                return ChoiceResult('unknown', 'Unknown choice.')
            allow = {int(x) for x in (allowlist or [])}
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                uid = None
            owner = row.get('user_id')
            if uid is None or uid not in allow or (owner is not None and uid != int(owner)):
                return ChoiceResult('foreign', 'Not your choice.')
            if row.get('spent') or self._group_chosen(row.get('group')):
                return ChoiceResult('spent', 'Already chosen.')
            expires = row.get('expires')
            if expires is not None and self._now() > float(expires):
                _settle_waiters(row.get('group'), _EXPIRED)
                return ChoiceResult('expired', 'That choice expired.')
            action = row.get('action')
            if action == 'page':
                markup = self._install_page(
                    group=row['group'],
                    chat_id=row.get('chat_id'),
                    user_id=row.get('user_id'),
                    prompt=row.get('prompt') or '',
                    options=row.get('options') or [],
                    meaning=row.get('meaning') or {},
                    message_id=row.get('message_id'),
                    page=int(row.get('value') or 0),
                    expires=row.get('expires'),
                )
                self.save()
                return ChoiceResult(
                    'page', '',
                    prompt=row.get('prompt') or '',
                    reply_markup=markup,
                    chat_id=row.get('chat_id'),
                    message_id=row.get('message_id'),
                    group=row.get('group'),
                    value=row.get('value'),
                    meaning=row.get('meaning') or {},
                )
            if action == 'drill':
                children = row.get('children') or []
                markup = self._install_page(
                    group=row['group'],
                    chat_id=row.get('chat_id'),
                    user_id=row.get('user_id'),
                    prompt=row.get('prompt') or '',
                    options=children,
                    meaning=row.get('meaning') or {},
                    message_id=row.get('message_id'),
                    page=0,
                    expires=row.get('expires'),
                )
                self.save()
                return ChoiceResult(
                    'drill', '',
                    prompt=row.get('prompt') or '',
                    reply_markup=markup,
                    chat_id=row.get('chat_id'),
                    message_id=row.get('message_id'),
                    group=row.get('group'),
                    label=row.get('label'),
                )
            self._spend_group(row.get('group'), chosen=callback_data)
            self.save()
            group = row.get('group')
            value = row.get('value')
            _settle_waiters(group, value)
            label = row.get('label') or str(row.get('value') or '')
            return ChoiceResult(
                'selected', '',
                statement=label,
                value=row.get('value'),
                label=label,
                meaning=row.get('meaning') or {},
                prompt=row.get('prompt') or '',
                chat_id=row.get('chat_id'),
                message_id=row.get('message_id'),
                group=row.get('group'),
            )

    def _group_chosen(self, group) -> bool:
        # Caller must hold self._lock.
        for row in self._buttons.values():
            if row.get('group') == group and row.get('chosen'):
                return True
        return False

    def _spend_group(self, group, chosen):
        # Caller must hold self._lock.
        for cid, row in self._buttons.items():
            if row.get('group') != group:
                continue
            row['spent'] = True
            if cid == chosen:
                row['chosen'] = True

    def callback_ids(self):
        with self._lock:
            return list(self._buttons.keys())
