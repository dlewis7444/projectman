"""Pure helpers behind the Settings → Agents page (B3): bridge install + doctor.

Headless; no GTK. The GUI page (settings_window._build_agents_page) is thin glue
over these.
"""
import os

import agents
from settings import Settings


# ── agent_bridge_source ───────────────────────────────────────────────────────

def test_bridge_source_found(tmp_path):
    app = tmp_path
    (app / 'bridges' / 'opencode').mkdir(parents=True)
    f = app / 'bridges' / 'opencode' / 'projectman.js'
    f.write_text('// bridge')
    assert agents.agent_bridge_source(str(app), 'opencode') == str(f)


def test_bridge_source_missing_file(tmp_path):
    assert agents.agent_bridge_source(str(tmp_path), 'opencode') is None


def test_bridge_source_unknown_agent(tmp_path):
    assert agents.agent_bridge_source(str(tmp_path), 'claude') is None


def test_real_repo_ships_opencode_bridge():
    """The actual repo tree carries the opencode bridge where the GUI expects."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert agents.agent_bridge_source(repo, 'opencode') is not None


def test_real_repo_ships_grok_bridge():
    """T-B4: the repo carries the grok bridge (the JSON hook definition) where
    the GUI 'Install bridge' button + install_agent_bridge expect it. With this
    present, the Settings → Agents page shows an Install-bridge row for grok
    automatically (it checks ``agent_bridge_source(...) is not None``)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = agents.agent_bridge_source(repo, 'grok')
    assert src is not None
    assert src.endswith(os.path.join('bridges', 'grok', 'projectman.json'))


def test_three_agents_registered_with_display_names():
    """T-B4: the registry now carries THREE builtins; the settings page + the
    sidebar submenu iterate ADAPTERS, so all three appear by construction."""
    ids = list(agents.ADAPTERS.keys())
    assert 'claude' in ids and 'opencode' in ids and 'grok' in ids
    assert len(ids) >= 3
    names = {aid: agents.ADAPTERS[aid].display_name for aid in ids}
    assert names['claude'] == 'Claude Code'
    assert names['opencode'] == 'opencode'
    assert names['grok'] == 'Grok Build'


# ── F12a/F12b: multi-file grok install + absolute-path rewrite ────────────────

_GROK_JSON_FIXTURE = (
    '{"hooks": {"Stop": [{"hooks": [{"type": "command", '
    '"command": "python3 ~/.grok/hooks/projectman-status.py", '
    '"timeout": 10}]}]}}'
)


def _app_with_grok_bridge(tmp_path, json_text=_GROK_JSON_FIXTURE,
                          script_text='#!/usr/bin/env python3\n'):
    app = tmp_path / 'app'
    (app / 'bridges' / 'grok').mkdir(parents=True)
    (app / 'bridges' / 'grok' / 'projectman.json').write_text(json_text)
    (app / 'bridges' / 'grok' / 'projectman-status.py').write_text(script_text)
    return app


def test_install_grok_bridge_lands_both_files(tmp_path):
    """F12a BINDING: the GUI install path lands BOTH grok files — the hook JSON
    AND the status script, script executable. (The pre-fix machinery copied
    only the JSON; the referenced script never landed via the GUI button.)"""
    app = _app_with_grok_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    result = agents.install_agent_bridge(str(app), 'grok', home=str(home))
    assert result == 'installed'
    json_dest = home / '.grok' / 'hooks' / 'projectman.json'
    script_dest = home / '.grok' / 'hooks' / 'projectman-status.py'
    assert json_dest.exists()
    assert script_dest.exists()
    assert os.access(str(script_dest), os.X_OK)   # exec bit set


def test_install_grok_bridge_rewrites_command_to_absolute(tmp_path):
    """F12b BINDING: the INSTALLED JSON carries the absolute script path —
    nothing relies on grok shell-expanding `~`."""
    import json as _json
    app = _app_with_grok_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'grok', home=str(home))
    installed = (home / '.grok' / 'hooks' / 'projectman.json').read_text()
    expected_abs = str(home / '.grok' / 'hooks' / 'projectman-status.py')
    data = _json.loads(installed)
    cmds = [c['command']
            for matchers in data['hooks'].values()
            for m in matchers for c in m['hooks']]
    assert cmds, 'no commands in installed JSON'
    for cmd in cmds:
        assert expected_abs in cmd
        assert '~' not in cmd
    # The SOURCE JSON in the app tree is untouched (stays portable).
    src = (app / 'bridges' / 'grok' / 'projectman.json').read_text()
    assert '~/.grok/hooks/projectman-status.py' in src


def test_repo_grok_json_stays_portable():
    """F12b: the repo copy keeps the portable `~` form (the rewrite is
    install-time only)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(repo, 'bridges', 'grok', 'projectman.json')).read()
    assert 'python3 ~/.grok/hooks/projectman-status.py' in src


def test_install_grok_bridge_idempotent_with_rewrite(tmp_path):
    """The transform keeps idempotency: second run is 'already' (transformed
    source compared against dest, not raw source)."""
    app = _app_with_grok_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    assert agents.install_agent_bridge(str(app), 'grok', home=str(home)) == 'installed'
    assert agents.install_agent_bridge(str(app), 'grok', home=str(home)) == 'already'


def test_install_grok_bridge_repairs_lost_exec_bit(tmp_path):
    """A dest script that lost its exec bit gets repaired (counts as a change)."""
    app = _app_with_grok_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'grok', home=str(home))
    script_dest = home / '.grok' / 'hooks' / 'projectman-status.py'
    os.chmod(str(script_dest), 0o644)   # strip the bit
    assert agents.install_agent_bridge(str(app), 'grok', home=str(home)) == 'installed'
    assert os.access(str(script_dest), os.X_OK)
    # And back to steady state.
    assert agents.install_agent_bridge(str(app), 'grok', home=str(home)) == 'already'


def test_install_grok_bridge_missing_script_is_missing_source(tmp_path):
    """All-or-nothing: a JSON-only app tree (the script absent) installs
    NOTHING — a partial bridge must never land."""
    app = tmp_path / 'app'
    (app / 'bridges' / 'grok').mkdir(parents=True)
    (app / 'bridges' / 'grok' / 'projectman.json').write_text(_GROK_JSON_FIXTURE)
    home = tmp_path / 'home'
    home.mkdir()
    assert agents.install_agent_bridge(str(app), 'grok', home=str(home)) == 'missing-source'
    assert not (home / '.grok' / 'hooks' / 'projectman.json').exists()


def test_install_real_repo_grok_bridge_end_to_end(tmp_path):
    """Install the REAL repo bridge into a temp home: both files land, the
    script is executable, the JSON commands are absolute."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = tmp_path / 'home'
    home.mkdir()
    assert agents.install_agent_bridge(repo, 'grok', home=str(home)) == 'installed'
    script_dest = home / '.grok' / 'hooks' / 'projectman-status.py'
    json_dest = home / '.grok' / 'hooks' / 'projectman.json'
    assert script_dest.exists() and os.access(str(script_dest), os.X_OK)
    assert str(script_dest) in json_dest.read_text()
    assert agents.install_agent_bridge(repo, 'grok', home=str(home)) == 'already'


# ── install_agent_bridge ──────────────────────────────────────────────────────

def _app_with_bridge(tmp_path, content='// bridge v1'):
    app = tmp_path / 'app'
    (app / 'bridges' / 'opencode').mkdir(parents=True)
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text(content)
    return app


def test_install_bridge_fresh(tmp_path):
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    result = agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    assert result == 'installed'
    dest = home / '.config' / 'opencode' / 'plugins' / 'projectman.js'
    assert dest.exists()
    assert dest.read_text() == '// bridge v1'


def test_install_bridge_idempotent(tmp_path):
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    # Second run with identical content → 'already'.
    assert agents.install_agent_bridge(str(app), 'opencode', home=str(home)) == 'already'


def test_install_bridge_updates_changed(tmp_path):
    app = _app_with_bridge(tmp_path, content='// v1')
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    # Change the source → next install copies again.
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text('// v2')
    assert agents.install_agent_bridge(str(app), 'opencode', home=str(home)) == 'installed'
    dest = home / '.config' / 'opencode' / 'plugins' / 'projectman.js'
    assert dest.read_text() == '// v2'


def test_install_bridge_no_bridge_agent(tmp_path):
    assert agents.install_agent_bridge(str(tmp_path), 'claude', home=str(tmp_path)) == 'no-bridge'


def test_install_bridge_missing_source(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    # app dir has no bridges/.
    assert agents.install_agent_bridge(str(tmp_path / 'app'), 'opencode',
                                       home=str(home)) == 'missing-source'


# ── bridge_state + bridge_button_labels (M-UX.8 / C5) ─────────────────────────

def test_bridge_state_no_bridge_agent(tmp_path):
    assert agents.bridge_state(str(tmp_path), 'claude', home=str(tmp_path)) == 'no-bridge'


def test_bridge_state_missing_source(tmp_path):
    assert agents.bridge_state(str(tmp_path / 'app'), 'opencode',
                               home=str(tmp_path)) == 'missing-source'


def test_bridge_state_stale_when_not_installed(tmp_path):
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    # Source present, nothing in home yet → stale (button: Install/Update).
    assert agents.bridge_state(str(app), 'opencode', home=str(home)) == 'stale'


def test_bridge_state_current_after_install(tmp_path):
    """BINDING (item 3): after install, manifest state is 'current' → the button
    becomes 'Reinstall' with 'Bridge installed ✓'."""
    app = _app_with_bridge(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    assert agents.bridge_state(str(app), 'opencode', home=str(home)) == 'current'
    label, subtitle = agents.bridge_button_labels('current')
    assert label == 'Reinstall'
    assert 'installed' in subtitle


def test_bridge_state_stale_when_content_drifts(tmp_path):
    app = _app_with_bridge(tmp_path, content='// v1')
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(str(app), 'opencode', home=str(home))
    assert agents.bridge_state(str(app), 'opencode', home=str(home)) == 'current'
    # Source changes → dest is now out of date.
    (app / 'bridges' / 'opencode' / 'projectman.js').write_text('// v2')
    assert agents.bridge_state(str(app), 'opencode', home=str(home)) == 'stale'


def test_bridge_state_grok_current_after_install(tmp_path):
    """grok's multi-file bridge (JSON + executable script, with the F12b
    absolute-path rewrite) reports 'current' once installed."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = tmp_path / 'home'
    home.mkdir()
    assert agents.bridge_state(repo, 'grok', home=str(home)) == 'stale'
    agents.install_agent_bridge(repo, 'grok', home=str(home))
    assert agents.bridge_state(repo, 'grok', home=str(home)) == 'current'


def test_bridge_state_grok_stale_on_lost_exec_bit(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = tmp_path / 'home'
    home.mkdir()
    agents.install_agent_bridge(repo, 'grok', home=str(home))
    script = home / '.grok' / 'hooks' / 'projectman-status.py'
    script.chmod(0o644)  # strip exec bit
    assert agents.bridge_state(repo, 'grok', home=str(home)) == 'stale'


def test_bridge_button_labels_all_states():
    assert agents.bridge_button_labels('current')[0] == 'Reinstall'
    assert agents.bridge_button_labels('stale')[0] == 'Update bridge'
    assert agents.bridge_button_labels('missing-source')[0] == 'Install bridge'
    assert agents.bridge_button_labels('no-bridge')[0] == 'Install bridge'


# ── agent_doctor ──────────────────────────────────────────────────────────────

def test_doctor_ok_reports_version():
    def fake_run(argv):
        assert argv == ['opencode', '--version']
        return (0, '1.16.2\n')
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is True
    assert detail == '1.16.2'


def test_doctor_uses_custom_binary():
    seen = {}

    def fake_run(argv):
        seen['argv'] = argv
        return (0, 'x')
    s = Settings(agents={'opencode': {'binary': '/opt/oc/opencode'}})
    agents.agent_doctor(s, 'opencode', run_fn=fake_run)
    assert seen['argv'][0] == '/opt/oc/opencode'


def test_doctor_claude_uses_resolved_binary():
    seen = {}

    def fake_run(argv):
        seen['argv'] = argv
        return (0, 'claude 2.1.170')
    s = Settings(claude_binary='/usr/local/bin/claude')
    ok, detail = agents.agent_doctor(s, 'claude', run_fn=fake_run)
    assert seen['argv'] == ['/usr/local/bin/claude', '--version']
    assert ok and 'claude' in detail


def test_doctor_not_runnable():
    def fake_run(argv):
        return None  # OSError under the hood
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is False
    assert 'not found' in detail or 'not runnable' in detail


def test_doctor_nonzero_exit():
    def fake_run(argv):
        return (1, '')
    ok, detail = agents.agent_doctor(Settings(), 'opencode', run_fn=fake_run)
    assert ok is False
