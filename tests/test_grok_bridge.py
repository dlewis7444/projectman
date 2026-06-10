"""Grok status bridge + install.sh TOML edit (P3 Part B, T-B3 / T-B5).

Headless; no GTK, no grok. T-B3 runs the committed bridge selftest (the
standing-harness rule) and also drives the bridge script directly for the
notification-no-write proof. T-B5 is a pure-function test of install.sh's
[compat.claude] TOML merger.
"""
import json
import os
import subprocess
import sys

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_DIR = os.path.join(REPO, 'bridges', 'grok')
STATUS_SCRIPT = os.path.join(BRIDGE_DIR, 'projectman-status.py')
SELFTEST = os.path.join(BRIDGE_DIR, 'selftest.py')


# ── T-B3: the committed selftest must pass (harness doesn't evaporate) ─────────

def test_grok_bridge_selftest_passes():
    """The committed selftest runs the real status script against synthetic
    event sequences and asserts file states. Exit 0 == all checks passed."""
    r = subprocess.run([sys.executable, SELFTEST], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, (
        f"selftest failed (rc={r.returncode}):\n{r.stdout}\n{r.stderr}")
    assert 'failed (' in r.stdout  # tally line present
    # No check reported FAIL.
    assert '  FAIL  ' not in r.stdout


# ── T-B3: drive the bridge directly — happy path + notification no-write ──────

def _emit(home, event, cwd, *, session='sess-1', payload=None):
    env = dict(os.environ)
    env['HOME'] = str(home)
    env['GROK_HOOK_EVENT'] = event
    env['GROK_WORKSPACE_ROOT'] = cwd
    env['GROK_SESSION_ID'] = session
    body = dict(payload or {})
    body.setdefault('hookEventName', event)
    subprocess.run([sys.executable, STATUS_SCRIPT], input=json.dumps(body),
                   env=env, text=True, timeout=10)


def _slug(cwd):
    out = ['-' if ch in '/.' else ch for ch in cwd]
    return ''.join(out).lstrip('-')


def _status_path(home, cwd):
    return os.path.join(str(home), '.ProjectMan', 'status', _slug(cwd) + '.json')


def _read(home, cwd):
    p = _status_path(home, cwd)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def test_happy_path_working_then_done(tmp_path):
    """T-B3: session_start → user_prompt_submit → stop drives done → working →
    done (a real turn shows working while running, done at stop)."""
    cwd = '/home/u/proj-grok'
    _emit(tmp_path, 'session_start', cwd, payload={'source': 'new'})
    assert _read(tmp_path, cwd)['state'] == 'done'
    _emit(tmp_path, 'user_prompt_submit', cwd, payload={'promptId': 'p1'})
    assert _read(tmp_path, cwd)['state'] == 'working'
    _emit(tmp_path, 'stop', cwd, payload={'reason': 'end_turn'})
    assert _read(tmp_path, cwd)['state'] == 'done'


def test_notification_between_events_changes_nothing(tmp_path):
    """T-B3 (sentinel no-write proof): the internal `notification` receipt that
    grok fires after every hook must NOT touch the status file. Seed a sentinel
    `event` the bridge would never emit; if it writes, `event` changes."""
    cwd = '/home/u/proj-grok'
    status_dir = os.path.join(str(tmp_path), '.ProjectMan', 'status')
    os.makedirs(status_dir, exist_ok=True)
    p = _status_path(tmp_path, cwd)
    sentinel = {'state': 'working', 'event': '__SENTINEL__', 'cwd': cwd,
                'ts': 1, 'session': 'seed'}
    with open(p, 'w') as f:
        json.dump(sentinel, f)
    _emit(tmp_path, 'notification', cwd,
          payload={'notificationType': 'xai_session', 'level': 'info'})
    after = _read(tmp_path, cwd)
    assert after == sentinel, 'notification wrote/changed the status file'


def test_notification_fresh_no_file_created(tmp_path):
    """A `notification` with no pre-existing file creates nothing."""
    cwd = '/home/u/proj-grok'
    _emit(tmp_path, 'notification', cwd, payload={'notificationType': 'xai_session'})
    assert not os.path.exists(_status_path(tmp_path, cwd))


def test_unknown_event_no_op(tmp_path):
    cwd = '/home/u/proj-grok'
    _emit(tmp_path, 'totally_unknown', cwd, payload={})
    assert not os.path.exists(_status_path(tmp_path, cwd))


def test_empty_slug_guard(tmp_path):
    """cwd '/' → empty slug → no bare '.json' write."""
    _emit(tmp_path, 'user_prompt_submit', '/', payload={})
    bare = os.path.join(str(tmp_path), '.ProjectMan', 'status', '.json')
    assert not os.path.exists(bare)


def test_session_end_removes_file(tmp_path):
    cwd = '/home/u/proj-grok'
    _emit(tmp_path, 'user_prompt_submit', cwd, payload={})
    assert os.path.exists(_status_path(tmp_path, cwd))
    _emit(tmp_path, 'session_end', cwd, payload={})
    assert not os.path.exists(_status_path(tmp_path, cwd))


def test_status_schema_matches_other_writers(tmp_path):
    """The schema keys match hook.js / opencode bridge: state, event, cwd, ts,
    session (+ optional tool)."""
    cwd = '/home/u/proj-grok'
    _emit(tmp_path, 'user_prompt_submit', cwd, session='S99',
          payload={'toolName': 'Bash'})
    s = _read(tmp_path, cwd)
    assert set(s) >= {'state', 'event', 'cwd', 'ts', 'session'}
    assert s['cwd'] == cwd
    assert s['session'] == 'S99'
    assert s['event'] == 'user_prompt_submit'
    assert s['tool'] == 'Bash'


def test_bridge_uses_same_slug_rule_as_hookjs():
    """The slug rule must stay byte-identical to hook.js's (shared status dir,
    M-P3.4 collision comment). Verify the python slug == the JS regex result on
    a path exercising both '/' and '.'."""
    cwd = '/home/u/.config/a.b/proj'
    # hook.js: cwd.replace(/[\/\.]/g, '-').replace(/^-+/, '')
    import re
    js_equiv = re.sub(r'^-+', '', re.sub(r'[/.]', '-', cwd))
    assert _slug(cwd) == js_equiv


# ── T-B5: install.sh's [compat.claude] TOML merger (pure function) ────────────

@pytest.fixture
def _compat():
    """Import the bridge's compat_toml merger (it lives under bridges/grok/)."""
    sys.path.insert(0, BRIDGE_DIR)
    try:
        import compat_toml
    finally:
        sys.path.pop(0)
    return compat_toml


def test_compat_creates_from_empty(_compat):
    out = _compat.merge_compat_claude_hooks_false('')
    assert '[compat.claude]' in out
    assert 'hooks = false' in out


def test_compat_creates_from_none(_compat):
    out = _compat.merge_compat_claude_hooks_false(None)
    assert '[compat.claude]' in out and 'hooks = false' in out


def test_compat_preserves_existing_user_keys(_compat):
    existing = (
        '[cli]\n'
        'auto_update = false\n'
        '\n'
        '[model.pool-qwen]\n'
        'model = "qwen3.5:9b"\n'
        'base_url = "http://localhost:11434/v1"\n'
        'api_key = "ollama"\n'
    )
    out = _compat.merge_compat_claude_hooks_false(existing)
    # Every existing key/section survives.
    assert '[cli]' in out
    assert 'auto_update = false' in out
    assert '[model.pool-qwen]' in out
    assert 'model = "qwen3.5:9b"' in out
    assert 'base_url = "http://localhost:11434/v1"' in out
    assert 'api_key = "ollama"' in out
    # And the compat section was added.
    assert '[compat.claude]' in out
    assert 'hooks = false' in out


def test_compat_updates_existing_hooks_true(_compat):
    existing = '[compat.claude]\nhooks = true\n'
    out = _compat.merge_compat_claude_hooks_false(existing)
    assert 'hooks = false' in out
    assert 'hooks = true' not in out
    # Only one compat.claude section.
    assert out.count('[compat.claude]') == 1


def test_compat_preserves_other_keys_in_existing_compat_section(_compat):
    existing = (
        '[compat.claude]\n'
        'hooks = true\n'
        'commands = true\n'
    )
    out = _compat.merge_compat_claude_hooks_false(existing)
    assert 'hooks = false' in out
    assert 'commands = true' in out  # sibling key preserved


def test_compat_idempotent(_compat):
    existing = '[compat.claude]\nhooks = false\n'
    out = _compat.merge_compat_claude_hooks_false(existing)
    # Already correct → unchanged.
    assert out == existing


def test_compat_inserts_hooks_into_section_without_it(_compat):
    existing = '[compat.claude]\ncommands = false\n'
    out = _compat.merge_compat_claude_hooks_false(existing)
    assert 'hooks = false' in out
    assert 'commands = false' in out
    assert out.count('[compat.claude]') == 1


def test_compat_does_not_touch_unrelated_compat_sections(_compat):
    existing = (
        '[compat.other]\n'
        'hooks = true\n'
        '\n'
        '[compat.claude]\n'
        'hooks = true\n'
    )
    out = _compat.merge_compat_claude_hooks_false(existing)
    # compat.other's hooks=true is untouched; compat.claude's flipped.
    assert '[compat.other]' in out
    # The other section keeps true; only claude's became false.
    other_idx = out.index('[compat.other]')
    claude_idx = out.index('[compat.claude]')
    other_block = out[other_idx:claude_idx]
    claude_block = out[claude_idx:]
    assert 'hooks = true' in other_block
    assert 'hooks = false' in claude_block
    assert 'hooks = true' not in claude_block


def test_compat_cli_creates_file_when_absent(tmp_path, _compat):
    """T-B5: the CLI entry creates the file (and parents) when absent."""
    path = tmp_path / 'sub' / 'config.toml'
    r = subprocess.run([sys.executable,
                        os.path.join(BRIDGE_DIR, 'compat_toml.py'), str(path)],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert r.stdout.strip() == 'installed'
    assert path.exists()
    text = path.read_text()
    assert '[compat.claude]' in text and 'hooks = false' in text
    # Second run is idempotent → 'already'.
    r2 = subprocess.run([sys.executable,
                         os.path.join(BRIDGE_DIR, 'compat_toml.py'), str(path)],
                        capture_output=True, text=True, timeout=10)
    assert r2.stdout.strip() == 'already'
