"""Approval broker: in-bot unix-socket server gating rw-turn mutations.

Grok does NOT fire PreToolUse hooks in headless ``grok -p`` mode
(probe-proven 2026-10-01 against grok 1.0.44), so the old hook + phone
keyboard design is dead. rw PAA turns now reach the host ONLY through the
bot-owned MCP server ``paa_mcp.py``: built-in mutators stay blocked by the
gate argv's deny rules, and the model's ``run_command`` calls land here as
``{'kind': 'review', token, tool, command, reason}`` requests.

Reviewer-final mode (the maintainer 2026-10-01): no Telegram prompts. The automated
reviewer (:func:`review_action`, one-shot read-only ``tool_policy='plan'``
headless call, no session) vets each requested action against the turn's
original prompt and its verdict IS the decision:

* IN-SCOPE   → the BROKER executes the command itself (``bash -c``,
  ``stdin=DEVNULL``, cwd = the PAA cwd, env scrubbed of the channel
  halves, output decoded ``errors='replace'``) and returns
  ``{'decision': 'allow', 'output': ...}``. The exec timeout is capped so
  review + exec + margin always fits under the MCP round-trip deadline
  (:meth:`ApprovalBroker._exec_cap`) — the server can never give up while
  the broker is still executing. The MCP server never executes anything.
* EXCEEDS / RISKY → ``{'decision': 'deny', 'reason': <verdict sentence>}``
  so the model can tell the user why.
* REVIEW-UNAVAILABLE → ``{'decision': 'deny', 'reason': 'review
  unavailable'}`` — fail CLOSED. This differs from the 2026-09-30 advisory
  semantics (verdict annotated a keyboard but never blocked): there is no
  human in this loop, so an unavailable reviewer means nothing runs.

Every reviewed command + verdict is appended to the PAA journal
(``paa-journal.md`` in the PAA cwd) — the audit trail.

Turn binding: each ``begin_turn`` mints a random token
(``secrets.token_urlsafe(16)``); ``channel()`` hands TurnService
``(sock_path, token)`` at turn start and the adapter injects both halves
into the child env (``PAA_APPROVAL_SOCK`` + ``PAA_APPROVAL_TOKEN``), which
the MCP server config passes through to its own process. Review requests
with a missing/wrong token are denied WITHOUT review.

The phone-keyboard approval machinery (``_process_approval``,
:func:`approval_text`, the pending-keyboard expiry in ``end_turn``) stays
intact but DORMANT — it is the future rw-with-prompts / auto-escalation
path and is not wired to MCP requests in this round.

Latency chain (M1a, agent-6 review): review ≤ REVIEW_DEADLINE_SEC (40s),
then the exec cap is ``min(EXEC_TIMEOUT_SEC, PAA_MCP_DEADLINE_SEC -
elapsed - EXEC_MARGIN_SEC)`` with a MIN_EXEC_SEC floor — so
review + exec + margin always completes inside the MCP round-trip
deadline the SERVER enforces (paa_mcp ``PAA_MCP_DEADLINE_SEC``, 120s
default), which sits under grok's ``tool_timeout_sec`` (180s). A review
that eats the budget denies with 'review consumed the budget; command
not run' BEFORE anything executes — that statement is only ever made
when it is true.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone

from paa_choices import waiter
from paa_mcp import DEFAULT_DEADLINE_SEC as MCP_DEADLINE_SEC

log = logging.getLogger('paa_approve')

HOOK_CONFIG_NAME = 'paa-approval.json'
# Dead hook path (probe-proven inert headless 2026-10-01). serve() retires
# the file the old self-install wrote; the name stays so remove-if-ours
# knows what to look for.
HOOK_DEADLINE_SEC = 110
REVIEW_DEADLINE_SEC = 40
REVIEW_JOIN_SLACK_SEC = 10
APPROVAL_TTL_SEC = 120
WAIT_MARGIN_SEC = 5
MIN_WAIT_SEC = 1.0
REQUEST_READ_SEC = 10
EXEC_TIMEOUT_SEC = 120
# Exec budget vs the MCP round-trip deadline (M1a): margin covers the
# socket connect/read on the server side; MIN_EXEC_SEC is the floor below
# which a command is not usefully executable and we deny instead.
EXEC_MARGIN_SEC = 5.0
MIN_EXEC_SEC = 10.0
# Channel halves (plus the deadline override) scrubbed from the executed
# command's environment (L2): a unit misconfigured with those exported
# must not arm grandchild MCP processes.
SCRUB_ENV_VARS = ('PAA_APPROVAL_SOCK', 'PAA_APPROVAL_TOKEN',
                  'PAA_MCP_DEADLINE_SEC')
# Broker-side output cap; paa_mcp.OUTPUT_MAX is the same contract on the
# wire side. Keep the two in sync (15 KiB).
OUTPUT_MAX = 15 * 1024
AUDIT_COMMAND_MAX = 200
AUDIT_REASON_MAX = 120
SOCK_NAME = 'paa-approve.sock'
SOCK_MODE = 0o700
SUMMARY_MAX = 500
# Review-path caps (item 1, 2026-10-01 hardening): the reviewer must see
# the FULL command, and the journal keeps it whole — a clipped prefix is
# how a destructive tail hides. Commands past REVIEW_INPUT_MAX are
# rejected without review; the user-prompt clip stays modest.
REVIEW_INPUT_MAX = 4000
JOURNAL_HASH_LEN = 12
TOOL_MAX = 100
PROMPT_CLIP = 1000
VERDICT_MAX = 200
# Keyboard message cap, comfortably under Telegram's 4096; the input
# summary is shrunk further to fit. (Dormant keyboard path.)
MESSAGE_MAX = 3800

VERDICT_TOKENS = ('IN-SCOPE', 'EXCEEDS', 'RISKY')
REVIEW_UNAVAILABLE = 'REVIEW-UNAVAILABLE'
# A verdict token must be followed by end-of-line or a non-letter, so
# "IN-SCOPEX" does not parse as IN-SCOPE.
_VERDICT_RE = re.compile(r'^(IN-SCOPE|EXCEEDS|RISKY)(?![A-Za-z])',
                         re.IGNORECASE)

_RECV_MAX = 1 << 20


# -- static pre-checks (before the model review) --------------------------------
#
# A small, tight, documented denylist enforced in the broker BEFORE any
# reviewer call (item 2, 2026-10-01 hardening). The static layer only ever
# blocks what it RECOGNIZES; anything it cannot parse is passed through to
# the reviewer. It is deliberately conservative: a blocked innocuous
# command costs a reworded retry, an allowed destructive one costs data.
# Over-blocking (e.g. `echo "rm -rf foo"` or a chmod on ~/.ssh) is the
# intended fail-closed behavior, not a bug.


_STATIC_DENY_REASON = 'blocked by static policy'
_STATIC_SECRET_PREFIXES = (
    '~/.ssh', '$HOME/.ssh', '~/.gnupg', '$HOME/.gnupg',
    '~/.password-store', '$HOME/.password-store',
)
_STATIC_PIPE_SHELL_RE = re.compile(
    r'\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash)\b')
_STATIC_DD_RE = re.compile(r'\bdd\b[^|;&]*\bof=')
_STATIC_MKFS_RE = re.compile(r'\bmkfs(?:\.[a-zA-Z0-9]+)?\b')
_STATIC_REDIRECT_RE = re.compile(r'(?:>>?|1>|2>|&>>?)\s*([^\s|;&]+)')
_STATIC_WRITE_OK_PREFIXES = (
    '/dev/null', '/dev/stdout', '/dev/stderr', '/dev/fd/', '/dev/pts/',
)


def _strip_quotes(token: str) -> str:
    return token.strip().strip('\'"')


def _rm_rf_segment(tokens) -> bool:
    """One simple-command segment: is it `rm` with a combined r+f flag?
    Matches -rf/-fr/-Rf/-fR and combined forms like -rfv."""
    if not tokens or os.path.basename(_strip_quotes(tokens[0])) != 'rm':
        return False
    for tok in tokens[1:]:
        low = tok.lower()
        if low.startswith('--'):
            continue
        if low.startswith('-') and 'r' in low and 'f' in low:
            return True
    return False


def _write_targets(command: str):
    """Best-effort extraction of write destinations: redirection targets
    and tee/cp/mv operands. Returns a list of raw target strings, or None
    when a target is unparseable ($VAR / command substitution) — the
    caller then PASSES the write check to the reviewer."""
    targets = []
    for match in _STATIC_REDIRECT_RE.finditer(command):
        target = match.group(1)
        if target.startswith('&'):      # fd plumbing: 2>&1
            continue
        targets.append(target)
    tokens = command.split()
    for index, tok in enumerate(tokens):
        base = os.path.basename(_strip_quotes(tok))
        if base == 'tee':
            for operand in tokens[index + 1:]:
                operand = _strip_quotes(operand)
                if operand.startswith('-') or operand in ('|', '&&', '||',
                                                          ';'):
                    continue
                targets.append(operand)
        elif base in ('cp', 'mv'):
            operands = [t for t in tokens[index + 1:]
                        if not _strip_quotes(t).startswith('-')
                        and t not in ('|', '&&', '||', ';')]
            if operands:
                targets.append(operands[-1])
    return targets


def static_block_rule(command: str, paa_cwd: str) -> str | None:
    """Return the denylist rule name this command matches, else None.

    Recognition-only: each rule is a textual shape, and the write rule
    declines to judge when a destination cannot be resolved statically.
    Denials use the fixed reason :data:`_STATIC_DENY_REASON`.
    """
    segments = re.split(r'&&|\|\||[;|]', command)
    for segment in segments:
        if _rm_rf_segment(segment.split()):
            return 'rm-rf'
    if _STATIC_PIPE_SHELL_RE.search(command):
        return 'pipe-to-shell'
    if _STATIC_DD_RE.search(command):
        return 'dd-write'
    if _STATIC_MKFS_RE.search(command):
        return 'mkfs'
    collapsed = re.sub(r'\s+', '', command)
    if ':(){:|:&};:' in collapsed:
        return 'fork-bomb'
    for token in command.split():
        word = _strip_quotes(token)
        if any(word.startswith(prefix) for prefix in _STATIC_SECRET_PREFIXES):
            return 'secret-path'
    targets = _write_targets(command)
    if targets is not None:
        root = os.path.realpath(paa_cwd)
        for target in targets:
            word = _strip_quotes(target)
            if '$' in word or '`' in word:
                return None  # unresolvable → pass to the reviewer
            expanded = os.path.expanduser(word)
            absolute = (expanded if os.path.isabs(expanded)
                        else os.path.join(root, expanded))
            absolute = os.path.normpath(absolute)
            if absolute == root or absolute.startswith(root + os.sep):
                continue
            if absolute == '/tmp' or absolute.startswith('/tmp' + os.sep):
                continue
            if any(absolute.startswith(p)
                   for p in _STATIC_WRITE_OK_PREFIXES):
                continue
            return 'write-outside-paa-cwd'
    return None


# -- advisory reviewer -------------------------------------------------------


def parse_verdict(text: str) -> str:
    """Normalize the reviewer's first non-empty line to a verdict string.

    The line must start with one of the verdict tokens (case-insensitive
    prefix, normalized to canonical uppercase). Anything else —
    empty text, prose, a missing token — is REVIEW-UNAVAILABLE.
    """
    if not isinstance(text, str):
        return REVIEW_UNAVAILABLE
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _VERDICT_RE.match(line)
        if match is None:
            return REVIEW_UNAVAILABLE
        token = match.group(1).upper()
        rest = line[match.end():].strip(' \t:-—–')
        return f'{token} — {rest}' if rest else token
    return REVIEW_UNAVAILABLE


def review_prompt(user_prompt: str, tool: str, action_summary: str, *,
                  denied_note: str | None = None) -> str:
    lines = [
        'REVIEW ACTION — your verdict is FINAL',
        f'User request: {user_prompt}',
        'The agent proposes this tool action:',
    ]
    if tool:
        lines.append(f'Tool: {tool}')
    lines.append(f'Input: {action_summary}')
    if denied_note:
        lines.append(denied_note)
    return (
        '\n'.join(lines) + '\n'
        'Answer with exactly one line. It must start with IN-SCOPE, '
        'EXCEEDS, or RISKY, followed by one short sentence.\n'
        'IN-SCOPE — the action directly serves the user request and is '
        'safe to run exactly as specified.\n'
        'EXCEEDS — the action goes beyond what the user asked for.\n'
        'RISKY — the action risks serious damage or data loss.\n'
        'You are the ONLY gate for this unlocked session: an IN-SCOPE '
        'verdict executes the command immediately with no human check. '
        'Default to denial on ambiguity.'
    )


def review_action(settings, action_summary, user_prompt, *,
                  chat_turn_fn, deadline_sec=REVIEW_DEADLINE_SEC,
                  project_path='', input_max=SUMMARY_MAX,
                  denied_note=None, reasoning_effort=None) -> str:
    """One-shot read-only verdict on the proposed action.

    Runs ``chat_turn_fn`` with ``tool_policy='plan'`` (deny-guarded,
    read-only) and NO session id, so the call cannot consume the scan
    budget or create/attach to a session. Never raises; any failure,
    hang past the deadline, empty reply, or harness error maps to
    REVIEW-UNAVAILABLE.

    Verdict-final semantics (2026-10-01): the reviewer is the sole gate
    for an unlocked rw session, so the CALLER treats REVIEW-UNAVAILABLE
    as denial (fail closed) — unlike the 2026-09-30 advisory mode where
    a missing verdict only annotated a keyboard.

    *input_max* — cap on the action summary in the prompt. The live MCP
    review path passes REVIEW_INPUT_MAX (4000) so the reviewer sees the
    full command; the dormant keyboard path keeps SUMMARY_MAX.
    *denied_note* — one extra line showing this turn's denial memory
    (item 3): the reviewer sees the reword-retry pattern.
    *reasoning_effort* — threaded to ``chat_turn_fn`` for harnesses that
    support it (grok ``--reasoning-effort``); None leaves argv untouched.
    """
    prompt = review_prompt(
        _clip(user_prompt, PROMPT_CLIP), '',
        _clip(action_summary, input_max), denied_note=denied_note)
    box: dict = {}

    def _run():
        try:
            box['result'] = chat_turn_fn(
                settings, prompt, session_id=None, project_path=project_path,
                tool_policy='plan', timeout=int(deadline_sec),
                reasoning_effort=reasoning_effort)
        except Exception as exc:  # noqa: BLE001 — reviewer must never raise
            box['error'] = exc

    thread = threading.Thread(target=_run, name='paa-review', daemon=True)
    thread.start()
    thread.join(deadline_sec + REVIEW_JOIN_SLACK_SEC)
    if thread.is_alive():
        log.warning('paa approval: reviewer ran past %ss; verdict %s',
                    deadline_sec, REVIEW_UNAVAILABLE)
        return REVIEW_UNAVAILABLE
    if 'error' in box:
        log.warning('paa approval: reviewer failed: %s', box['error'])
        return REVIEW_UNAVAILABLE
    result = box.get('result')
    text = getattr(result, 'text', None)
    error = getattr(result, 'error', None)
    if not text or error:
        log.info('paa approval: reviewer unavailable: %s',
                 error or 'empty reply')
        return REVIEW_UNAVAILABLE
    return parse_verdict(text)


# -- dead hook config retirement -------------------------------------------------


def _is_our_hook_config(text: str) -> bool:
    """True when *text* is the paa-approval.json this bot used to install.

    Content marker (never the filename alone): the JSON parses and some
    PreToolUse hook command names ``paa_grok_hook.py``. Anything else is
    a foreign file we must not touch.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    pre = data.get('hooks')
    if not isinstance(pre, dict):
        return False
    entries = pre.get('PreToolUse')
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get('hooks') or []:
            if isinstance(hook, dict) and 'paa_grok_hook.py' in str(
                    hook.get('command') or ''):
                return True
    return False


def remove_hook_config(hooks_dir: str | None = None) -> tuple:
    """Retire the dead PreToolUse hook config — but ONLY if it is ours.

    Grok never fires PreToolUse hooks in headless ``grok -p`` mode
    (probe-proven 2026-10-01), so the hook-based gate cannot enforce
    anything and the self-installed ``~/.grok/hooks/paa-approval.json``
    is retired here, at serve() time, by content marker: the file is
    deleted only when it parses as a config whose hook command names
    ``paa_grok_hook.py``. Foreign content is left alone. Nothing else in
    the hooks dir is touched. Returns ``(path, removed)`` where *removed*
    is True (deleted), False (present but not ours), or None (absent).
    """
    if hooks_dir is None:
        hooks_dir = os.path.join(os.path.expanduser('~'), '.grok', 'hooks')
    path = os.path.join(hooks_dir, HOOK_CONFIG_NAME)
    try:
        with open(path, 'r') as fh:
            current = fh.read()
    except FileNotFoundError:
        return path, None
    except OSError:
        log.warning('paa approval: cannot read %s', path, exc_info=True)
        return path, False
    if not _is_our_hook_config(current):
        return path, False
    try:
        os.remove(path)
    except OSError:
        log.warning('paa approval: cannot remove %s', path, exc_info=True)
        return path, False
    return path, True


# -- text ----------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = ' '.join(str(text or '').split())
    if len(text) > limit:
        keep = max(0, limit - 3)
        text = text[:keep] + ('...' if keep else '')
    return text


def _clip_tail(text: str, limit: int = OUTPUT_MAX) -> str:
    """Keep the TAIL of a long output (the end carries the result), with
    a '...' marker when truncated. One review line per journal record
    depends on newlines surviving, so newlines are NOT collapsed here."""
    text = str(text or '')
    if len(text) > limit:
        return '...\n' + text[-(limit - 4):]
    return text


def approval_text(tool: str, summary: str, user_prompt: str, verdict: str) -> str:
    """The keyboard message body; also the choice prompt (stored, never on buttons).

    The verdict line is clipped to ``VERDICT_MAX`` and the whole message to
    ``MESSAGE_MAX`` (under Telegram's 4096) by shrinking the input summary
    further — the tool, request, and verdict lines always survive.
    """
    verdict = _clip(verdict, VERDICT_MAX)

    def build(summary_clip):
        return '\n'.join([
            'Approval requested',
            f'Tool: {_clip(tool, TOOL_MAX) or "?"}',
            f'Input: {_clip(summary, summary_clip) or "(none)"}',
            f'Request: {_clip(user_prompt, PROMPT_CLIP) or "(none)"}',
            f'Review: {verdict}',
        ])

    text = build(SUMMARY_MAX)
    if len(text) > MESSAGE_MAX:
        text = build(max(0, SUMMARY_MAX - (len(text) - MESSAGE_MAX) - 4))
    return text


def _deny(reason: str) -> dict:
    return {'decision': 'deny', 'reason': reason}


# -- broker --------------------------------------------------------------------


class ApprovalBroker:
    """AF_UNIX listener bridging MCP review requests to the reviewer.

    One daemon accept thread (the :class:`TypingSender` pattern); each
    connection is handled on its own daemon thread. ``begin_turn`` /
    ``end_turn`` bracket the actually-running gate turn from
    ``TurnService._run_prompt`` (under the turn worker, after the lock
    check): no active turn → every request is denied, and turn end — in a
    ``finally`` around ``chat_turn``, so error results, exceptions, and
    /stop-kills all pair — revokes the turn (and, for the dormant keyboard
    path, the blanket auto-allow and pending approval keyboards).

    Two request shapes share the socket. ``{'kind': 'review', ...}``
    (paa_mcp.py) is the LIVE path: reviewer-final, broker executes on
    IN-SCOPE. The hook-shaped approval request (``toolUseId`` + ``input``)
    is the DORMANT phone-keyboard path kept for the future
    rw-with-prompts mode; nothing sends it in this round.
    """

    def __init__(self, state_dir, api, service, *, chat_turn_fn=None,
                 sock_name=SOCK_NAME, now_fn=None, ttl_sec=None,
                 review_deadline_sec=None, hook_deadline_sec=None,
                 mcp_deadline_sec=None, audit_path=None):
        self._state_dir = state_dir
        self.sock_path = os.path.join(state_dir, sock_name)
        self._api = api
        self._service = service
        self._chat_turn = chat_turn_fn
        self._now = now_fn or time.time
        self.ttl_sec = float(ttl_sec if ttl_sec is not None
                             else (os.environ.get('PAA_APPROVAL_TTL_SEC')
                                   or APPROVAL_TTL_SEC))
        self.review_deadline_sec = float(
            review_deadline_sec if review_deadline_sec is not None
            else REVIEW_DEADLINE_SEC)
        self.hook_deadline_sec = float(
            hook_deadline_sec if hook_deadline_sec is not None
            else (os.environ.get('PAA_APPROVAL_DEADLINE_SEC')
                  or HOOK_DEADLINE_SEC))
        # The MCP round-trip deadline the SERVER enforces (M1a). The exec
        # cap derived from it keeps the whole response inside this budget,
        # so the server never reports 'not run' while we are still
        # executing. Same env var as the server: one knob, both sides.
        self.mcp_deadline_sec = float(
            mcp_deadline_sec if mcp_deadline_sec is not None
            else (os.environ.get('PAA_MCP_DEADLINE_SEC')
                  or MCP_DEADLINE_SEC))
        # Audit trail target; None → <PAA cwd>/paa-journal.md, computed
        # lazily so construction never depends on settings shape.
        self._audit_path = audit_path
        self._lock = threading.Lock()
        self._review_lock = threading.Lock()
        self._active: dict | None = None
        self._pending: set = set()
        self._last_verdict: str | None = None
        # Per-turn denial memory (item 3): (normalized command, outcome)
        # pairs, so an exact repeat denies without a new review and a
        # reworded retry shows the reviewer the pattern. Cleared at
        # begin/end of every turn.
        self._denied: list = []
        self._listener = None
        self._thread = None
        self._stopping = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.sock_path)
        os.chmod(self.sock_path, SOCK_MODE)
        listener.listen(8)
        self._listener = listener
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._serve, name='paa-approve', daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning('paa approval: cannot remove %s',
                        self.sock_path, exc_info=True)

    def _serve(self):
        while not self._stopping.is_set():
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                if self._stopping.is_set():
                    break
                log.exception('paa approval: accept failed')
                continue
            threading.Thread(
                target=self._handle_conn, args=(conn,),
                daemon=True).start()

    # -- active-turn scoping (called by TurnService._run_prompt) ----------------

    def begin_turn(self, chat_id, user_id, prompt) -> str:
        """Register the owning follow and mint its per-turn token.

        The token is the review channel's second half: only requests
        bearing it get reviewed (or, on the dormant keyboard path,
        keyboards and the blanket auto-allow). It dies with the turn —
        ``end_turn`` drops the whole record.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._active = {
                'chat_id': chat_id,
                'user_id': user_id,
                'prompt': prompt or '',
                'blanket': False,
                'token': token,
            }
            # Fresh turn: fresh denial memory.
            self._denied = []
        return token

    def channel(self):
        """Approval channel for the active turn: ``(sock_path, token)``.

        TurnService's ``approval_sock_provider`` contract; None when no
        turn is active (the rw turn is then refused, never run un-gated).
        """
        with self._lock:
            active = self._active
            if active is None:
                return None
            return (self.sock_path, active['token'])

    def alive(self) -> bool:
        """Liveness probe for /status: exists → S_ISSOCK → connect.

        Independent of any active turn (unlike :meth:`channel`), so the
        phone can see the broker is healthy between turns. The empty
        probe connection is tolerated by the accept loop.
        """
        try:
            info = os.stat(self.sock_path)
        except OSError:
            return False
        if not stat.S_ISSOCK(info.st_mode):
            return False
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(2.0)
                probe.connect(self.sock_path)
            finally:
                probe.close()
        except OSError:
            return False
        return True

    def end_turn(self):
        # Revoke the turn first so no new request can validate, clear the
        # per-turn denial memory with it, then expire pending approval
        # keyboards. The loop guards the snapshot race with a request that
        # creates its group between passes: a keyboard created
        # concurrently with end_turn cannot linger.
        with self._lock:
            self._active = None
            self._denied = []
        for _pass in range(3):
            with self._lock:
                pending = list(self._pending)
            live = [group for group in pending
                    if self._service.choices.group_outcome(group) is None]
            if not live:
                return
            for group in live:
                self._service.choices.expire_group(group)
        log.warning('paa approval: pending keyboards remain after 3 passes')

    @property
    def last_verdict(self):
        with self._lock:
            return self._last_verdict

    def _record_verdict(self, verdict):
        with self._lock:
            self._last_verdict = verdict

    # -- request path ----------------------------------------------------------

    def _handle_conn(self, conn):
        try:
            conn.settimeout(REQUEST_READ_SEC)
            buf = b''
            while b'\n' not in buf and len(buf) <= _RECV_MAX:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b'\n', 1)[0].strip()
            if not line:
                # Empty probe connection: the rw-turn liveness check
                # (exists → S_ISSOCK → connect probe) or a health ping.
                return
            request = json.loads(line.decode('utf-8', 'replace'))
            if not isinstance(request, dict):
                raise ValueError('request is not a JSON object')
        except (OSError, ValueError) as exc:
            log.debug('paa approval: ignored bad connection: %s', exc)
            try:
                conn.close()
            except OSError:
                pass
            return
        try:
            response = self._process(request)
        except Exception:
            log.exception('paa approval: request failed')
            response = _deny('approval unavailable')
        try:
            conn.sendall((json.dumps(response) + '\n').encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _process(self, request: dict) -> dict:
        if request.get('kind') == 'review':
            return self._process_review(request)
        return self._process_approval(request)

    # -- MCP review path (LIVE): paa_mcp.py run_command --------------------------

    def _process_review(self, request: dict) -> dict:
        """Reviewer-final gate for one ``run_command`` call.

        Token validation happens WITHOUT review (a wrong/missing token is
        denied outright — no reviewer spend, no side effects). Valid
        requests serialize on ``_review_lock``: one review at a time, a
        concurrent request is told the channel is busy rather than queued
        behind an unrelated command. The whole method runs inside the MCP
        round-trip deadline (started at entry), so the exec cap derived
        after the review keeps the reply inside the server's budget (M1a).
        """
        started = time.monotonic()
        with self._lock:
            active = self._active
            if active is None:
                return _deny('no active approval turn')
            if not secrets.compare_digest(
                    str(request.get('token') or ''),
                    str(active.get('token') or '')):
                return _deny('unknown turn')
            user_prompt = active.get('prompt') or ''
        command = str(request.get('command') or '')
        if not command.strip():
            return _deny('empty command')
        # Length gate (item 1): the reviewer must see the WHOLE command;
        # past the cap we cannot vet it, so nothing runs.
        if len(command) > REVIEW_INPUT_MAX:
            reason = 'command too long to review'
            self._audit('—', command, str(request.get('reason') or ''),
                        f'deny ({reason})')
            self._remember_denied(command, reason)
            return _deny(reason)
        # Static denylist (item 2), before any reviewer spend. The layer
        # only blocks what it recognizes; everything else reaches the
        # reviewer.
        from paa_headless import default_paa_cwd
        static_rule = static_block_rule(
            command, default_paa_cwd(self._service.settings))
        if static_rule is not None:
            self._audit('—', command, str(request.get('reason') or ''),
                        f'deny (static policy: {static_rule})')
            self._remember_denied(command, _STATIC_DENY_REASON)
            return _deny(_STATIC_DENY_REASON)
        # Denial memory (item 3): an exact normalized repeat denies
        # without a new review; anything else carries the memory into the
        # review prompt so the reviewer sees the reword-retry pattern.
        normalized = ' '.join(command.split())
        with self._lock:
            denied_snapshot = list(self._denied)
        for stored, outcome in denied_snapshot:
            if stored == normalized:
                self._audit('—', command,
                            str(request.get('reason') or ''),
                            'deny (repeat)')
                return _deny('already denied this turn')
        denied_note = None
        if denied_snapshot:
            last_cmd, last_outcome = denied_snapshot[-1]
            denied_note = (
                f'Note: {len(denied_snapshot)} command(s) were already '
                f'denied this turn, most recently: '
                f'{_clip(last_cmd, AUDIT_COMMAND_MAX)} ({last_outcome})')
        if not self._review_lock.acquire(blocking=False):
            return _deny('another review is in progress')
        try:
            tool = str(request.get('tool') or '')
            reason = str(request.get('reason') or '')
            # The envelope clips around the command stay small so the
            # command itself always fits inside input_max (item 1): a
            # 4000-char command is never clipped by its own prefix.
            summary = (f'{tool}: {command}\n'
                       f'Reason given: {_clip(reason, 400)}')
            config = getattr(self._service, 'config', None) or {}
            effort = str(config.get('reviewer_effort') or 'low')
            verdict = REVIEW_UNAVAILABLE
            if self._chat_turn is not None:
                verdict = review_action(
                    self._service.settings, summary, user_prompt,
                    chat_turn_fn=self._chat_turn,
                    deadline_sec=self.review_deadline_sec,
                    input_max=REVIEW_INPUT_MAX + 600,
                    denied_note=denied_note,
                    reasoning_effort=effort)
            self._record_verdict(verdict)
            token = verdict.split(' ', 1)[0] if isinstance(
                verdict, str) else REVIEW_UNAVAILABLE
            if token == 'IN-SCOPE':
                cap, budget_reason = self._exec_cap(
                    time.monotonic() - started)
                if budget_reason is not None:
                    # True by construction: execution has not started.
                    self._audit(verdict, command, reason,
                                f'deny ({budget_reason})')
                    self._remember_denied(command, budget_reason)
                    return _deny(budget_reason)
                output, error = self._execute(command, timeout=cap)
                if error is not None:
                    self._audit(verdict, command, reason,
                                f'deny ({error})')
                    self._remember_denied(command, error)
                    return _deny(error)
                self._audit(verdict, command, reason, 'allow')
                return {'decision': 'allow', 'output': output}
            if token in ('EXCEEDS', 'RISKY'):
                # The model needs the WHY so it can report to the maintainer.
                self._audit(verdict, command, reason, 'deny')
                self._remember_denied(command, verdict)
                return _deny(verdict)
            self._audit(REVIEW_UNAVAILABLE, command, reason,
                        'deny (review unavailable)')
            self._remember_denied(command, 'review unavailable')
            return _deny('review unavailable')
        finally:
            self._review_lock.release()

    def _remember_denied(self, command, outcome):
        """Append to the per-turn denial memory (whitespace-collapsed)."""
        with self._lock:
            self._denied.append((' '.join(command.split()), outcome))

    def _exec_cap(self, elapsed):
        """Exec timeout that keeps review+exec+margin under the MCP
        round-trip deadline the server enforces (M1a).

        Returns ``(cap_seconds, None)`` or ``(None, deny_reason)``. The
        deny branch is only ever taken BEFORE :meth:`_execute` runs, so
        'command not run' is always a true statement. Default chain:
        review 40s leaves 120−40−5 = 75s of exec ≥ MIN_EXEC_SEC floor.
        """
        remaining = self.mcp_deadline_sec - elapsed - EXEC_MARGIN_SEC
        if remaining < MIN_EXEC_SEC:
            return None, 'review consumed the budget; command not run'
        return min(EXEC_TIMEOUT_SEC, remaining), None

    def _execute(self, command, *, timeout=None):
        """Run one vetted command IN THE BROKER process.

        ``bash -c``, stdin DEVNULL, cwd = the PAA cwd (never the bot's
        cwd), env scrubbed of the channel halves (L2), output decoded with
        ``errors='replace'`` so binary output cannot raise past the guard
        (M1b). Returns ``(output, None)`` with combined stdout+stderr
        (tail-truncated to OUTPUT_MAX, an ``[exit N]`` header when
        nonzero), or ``(None, error)`` when the command did not run
        (timeout kill, spawn/setup failure). If the command RAN but its
        output cannot be delivered, the truth is reported instead of a
        misleading deny: ``( '[executed; output unavailable: …]', None)``.
        The MCP server never executes anything.
        """
        try:
            from paa_headless import default_paa_cwd
            cwd = default_paa_cwd(self._service.settings)
            env = {k: v for k, v in os.environ.items()
                   if k not in SCRUB_ENV_VARS}
        except Exception as exc:  # noqa: BLE001 — setup failed, nothing ran
            return None, f'command not run: {exc}'
        try:
            proc = subprocess.run(
                ['bash', '-c', command], cwd=cwd, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, errors='replace',
                timeout=timeout if timeout is not None else EXEC_TIMEOUT_SEC,
                env=env)
        except subprocess.TimeoutExpired:
            log.warning('paa approval: command timed out: %s',
                        _clip(command, AUDIT_COMMAND_MAX))
            return None, 'command timed out'
        except OSError as exc:
            return None, f'command failed to start: {exc}'
        try:
            combined = (proc.stdout or '') + (proc.stderr or '')
            header = '' if proc.returncode == 0 \
                else f'[exit {proc.returncode}]\n'
            return _clip_tail(header + combined), None
        except Exception as exc:  # noqa: BLE001 — the command RAN; never
            # claim otherwise. A deny here would invite a retry that
            # double-executes.
            log.exception('paa approval: output handling failed after exec')
            return (f'[executed; output unavailable: '
                    f'{type(exc).__name__}: {exc}]'), None

    def _audit_file(self):
        if self._audit_path is not None:
            return self._audit_path
        try:
            from paa_headless import default_paa_cwd
            return os.path.join(
                default_paa_cwd(self._service.settings), 'paa-journal.md')
        except Exception:  # noqa: BLE001 — audit must never break a review
            log.debug('paa approval: no audit path', exc_info=True)
            return None

    def _audit(self, verdict, command, reason, outcome):
        """Append one reviewed-command record to the PAA journal.

        The command is logged IN FULL (newlines/tab/backslash escaped so
        the entry stays one line) — the audit trail must not hide a
        destructive tail behind a 200-char prefix. A short sha256 prefix
        keeps long entries greppable. Pre-review denials pass verdict
        ``'—'`` and get no verdict tail.
        """
        path = self._audit_file()
        if not path:
            return
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        digest = hashlib.sha256(
            command.encode('utf-8', 'replace')).hexdigest()
        shown = (command.replace('\\', '\\\\')
                 .replace('\n', '\\n').replace('\t', '\\t'))
        line = (
            f'- {stamp} mcp-review {outcome} '
            f'sha256={digest[:JOURNAL_HASH_LEN]}: `{shown}`'
            + (f' (reason: {_clip(reason, AUDIT_REASON_MAX)})'
               if reason else '')
            + (f' — {_clip(verdict, VERDICT_MAX)}'
               if verdict and verdict != '—' else '')
            + '\n'
        )
        try:
            with open(path, 'a') as fh:
                fh.write(line)
        except OSError:
            log.warning('paa approval: audit write failed to %s',
                        path, exc_info=True)

    # -- dormant phone-keyboard path (future rw-with-prompts mode) ---------------

    def _process_approval(self, request: dict) -> dict:
        with self._lock:
            active = self._active
            if active is None:
                return _deny('no active approval turn')
            # Turn binding: a missing or wrong token gets no keyboard. The
            # blanket also only applies to the same valid per-turn token
            # (a new turn mints a new one, so this check is sufficient).
            if not secrets.compare_digest(
                    str(request.get('token') or ''),
                    str(active.get('token') or '')):
                return _deny('unknown turn')
            if active['blanket']:
                return {'decision': 'allow'}
            chat_id = active['chat_id']
            user_id = active.get('user_id')
            user_prompt = active.get('prompt') or ''

        tool = str(request.get('tool') or '')
        summary = _clip(request.get('input') or '', SUMMARY_MAX)

        started = time.monotonic()
        verdict = REVIEW_UNAVAILABLE
        if self._chat_turn is not None:
            verdict = review_action(
                self._service.settings, summary, user_prompt,
                chat_turn_fn=self._chat_turn,
                deadline_sec=self.review_deadline_sec)
        self._record_verdict(verdict)
        # Stay inside the hook's overall deadline: review time eats the
        # wait budget, never the other way around.
        if self.hook_deadline_sec - (time.monotonic() - started) \
                - WAIT_MARGIN_SEC < MIN_WAIT_SEC:
            return _deny('approval timed out')

        text = approval_text(tool, summary, user_prompt, verdict)
        group, _prompt, markup = self._service.choices.create(
            chat_id=chat_id, user_id=user_id, prompt=text,
            options=[
                {'label': 'Allow once', 'value': 'allow'},
                {'label': 'Allow this turn', 'value': 'allow-turn'},
                {'label': 'Deny', 'value': 'deny'},
            ],
            meaning={'kind': 'approval',
                     'tool_use_id': str(request.get('toolUseId') or '')},
        )
        with self._lock:
            self._pending.add(group)
        try:
            sent = self._api.call(
                'sendMessage',
                {'chat_id': chat_id, 'text': text, 'reply_markup': markup},
                quiet=True)
        except Exception:
            log.exception('paa approval: cannot post keyboard')
            self._service.choices.expire_group(group)
            with self._lock:
                self._pending.discard(group)
            return _deny('approval unavailable')
        mid = None
        if isinstance(sent, dict):
            mid = (sent.get('result') or {}).get('message_id')
        if mid is not None:
            self._service.choices.bind_message(group, mid)

        # The send itself can be slow (Telegram hiccup): recompute the
        # budget AFTER it so review + send + wait never exceeds the hook
        # deadline minus margin. A wait that no longer fits denies and
        # expires the just-posted keyboard immediately.
        remaining = self.hook_deadline_sec - (time.monotonic() - started) \
            - WAIT_MARGIN_SEC
        if remaining < MIN_WAIT_SEC:
            self._expire_keyboard(group, chat_id, text, expired=False)
            return _deny('approval timed out')
        handle = waiter(group)
        value = handle.wait(min(self.ttl_sec, remaining))
        with self._lock:
            self._pending.discard(group)
        if value is None:
            expired = handle.expired or (
                self._service.choices.group_outcome(group) == ('expired', None))
            self._expire_keyboard(group, chat_id, text, expired)
            reason = 'approval expired' if expired else 'approval timed out'
            return _deny(reason)
        if value == 'allow':
            return {'decision': 'allow'}
        if value == 'allow-turn':
            with self._lock:
                if self._active is not None:
                    self._active['blanket'] = True
            return {'decision': 'allow'}
        return _deny('denied by user')

    def _expire_keyboard(self, group, chat_id, text, expired):
        self._service.choices.expire_group(group)
        mid = self._service.choices.group_message_id(group)
        if mid is None:
            return
        suffix = '(expired)' if expired else '(timed out)'
        try:
            self._api.call('editMessageText', {
                'chat_id': chat_id,
                'message_id': mid,
                'text': f'{text}\n{suffix}',
                'reply_markup': {'inline_keyboard': []},
            }, quiet=True)
        except Exception:
            log.debug('paa approval: cannot mark keyboard expired',
                      exc_info=True)
