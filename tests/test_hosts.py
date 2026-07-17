"""Phase 0 host axis — project_ref, HostProfile, override key migration."""
import json
import os

import pytest

from hosts import (
    LOCALHOST_ID,
    HostProfile,
    BinarySpec,
    encode_project_ref,
    decode_project_ref,
    normalize_override_key,
    migrate_override_map,
    lookup_override,
    parse_hosts_map,
    hosts_to_settings_dict,
    new_host_id,
    is_safe_project_name,
)
from settings import Settings
from session import save_session, load_session, load_harnesses, load_hosts
from model import Project


# ── project_ref ───────────────────────────────────────────────────────────────

def test_encode_localhost():
    assert encode_project_ref(LOCALHOST_ID, '/home/u/p') == 'local:/home/u/p'
    assert encode_project_ref('', '/home/u/p') == 'local:/home/u/p'


def test_encode_remote():
    assert encode_project_ref('abc123', 'myproj') == 'ssh:abc123:myproj'


def test_decode_local_prefix():
    assert decode_project_ref('local:/home/u/p') == (LOCALHOST_ID, '/home/u/p')


def test_decode_ssh():
    assert decode_project_ref('ssh:abc123:myproj') == ('abc123', 'myproj')


def test_decode_legacy_bare_path():
    assert decode_project_ref('/home/u/projects/foo') == (
        LOCALHOST_ID, '/home/u/projects/foo')


def test_decode_ssh_name_with_extra_colons():
    # name half may theoretically contain colons after first split
    assert decode_project_ref('ssh:hid:foo:bar') == ('hid', 'foo:bar')


def test_normalize_and_migrate_map():
    raw = {
        '/abs/a': 'claude',
        'local:/abs/b': 'grok',
        'ssh:h1:n': 'opencode',
    }
    m = migrate_override_map(raw)
    assert m['local:/abs/a'] == 'claude'
    assert m['local:/abs/b'] == 'grok'
    assert m['ssh:h1:n'] == 'opencode'
    assert '/abs/a' not in m


def test_lookup_override_namespaced_and_legacy():
    m = {'local:/p/a': 'x', '/p/b': 'y', 'ssh:h1:n': 'z'}
    assert lookup_override(m, LOCALHOST_ID, '/p/a') == ('x', True)
    assert lookup_override(m, LOCALHOST_ID, '/p/b') == ('y', True)
    assert lookup_override(m, 'h1', 'n') == ('z', True)
    assert lookup_override(m, LOCALHOST_ID, '/missing') == (None, False)


def test_lookup_override_accepts_full_project_ref_without_double_encode():
    """Remote path keys are already ``ssh:host:name`` — must not re-encode."""
    from hosts import override_key
    ref = override_key('myproj', 'h1')
    assert ref == 'ssh:h1:myproj'
    m = {ref: 'opencode'}
    # Callers often pass project.path (== ref) + host_id together.
    assert lookup_override(m, 'h1', ref) == ('opencode', True)
    assert lookup_override(m, 'h1', 'myproj') == ('opencode', True)


def test_filter_remote_export_env_strips_home():
    from ssh_transport import filter_remote_export_env
    env = {
        'HOME': '/home/user',
        'USER': 'dlewis',
        'PATH': '/usr/bin',
        'ANTHROPIC_BASE_URL': 'http://x',
        'ANTHROPIC_AUTH_TOKEN': 'k',
        'DISABLE_AUTOUPDATER': '1',
        'CLAUDE_CODE_MAX_CONTEXT_TOKENS': '200000',
        'SECRET_JUNK': 'nope',
    }
    out = filter_remote_export_env(env)
    assert 'HOME' not in out and 'USER' not in out and 'PATH' not in out
    assert out['ANTHROPIC_BASE_URL'] == 'http://x'
    assert out['DISABLE_AUTOUPDATER'] == '1'
    assert 'SECRET_JUNK' not in out


def test_remote_status_cwd_maps_to_project_name():
    """Only …/.ProjectMan/projects/<name> cwds map; bare home does not."""
    marker = '/.ProjectMan/projects/'

    def name_from(cwd):
        cwd = (cwd or '').replace('\\', '/')
        if marker not in cwd:
            return None
        rest = cwd.split(marker, 1)[1]
        name = rest.strip('/').split('/')[0]
        if not name or name.startswith('.'):
            return None
        return name

    assert name_from('/home/user/.ProjectMan/projects/test') == 'test'
    assert name_from('/home/user/.ProjectMan/projects/test/subdir') == 'test'
    assert name_from('/home/user/') is None
    assert name_from('/win/ssd2TB/CHATBOTS/personaplex') is None


# ── HostProfile ───────────────────────────────────────────────────────────────

def test_host_profile_title_prefers_name():
    p = HostProfile(id='h1', ssh_target='user@box.example', display_name='Cage')
    assert p.title() == 'Cage'
    p2 = HostProfile(id='h1', ssh_target='myremote')
    assert p2.title() == 'myremote'


def test_binary_spec_path_vs_override():
    p = HostProfile(
        id='h1', ssh_target='box',
        binaries={'claude': {'use_path': True, 'override': '/x'}},
    )
    assert p.binary_spec('claude').resolved('claude') == 'claude'
    p.binaries['claude'] = {'use_path': False, 'override': '/opt/claude'}
    assert p.binary_spec('claude').resolved() == '/opt/claude'


def test_parse_hosts_map_skips_bad():
    raw = {
        'good': {
            'id': 'good', 'ssh_target': 'cage', 'display_name': 'C',
        },
        'bad': {'ssh_target': ''},
        'localhost': {'id': 'localhost', 'ssh_target': 'nope'},
    }
    m = parse_hosts_map(raw)
    assert list(m) == ['good']
    assert m['good'].display_name == 'C'


def test_new_host_id_unique():
    assert new_host_id() != new_host_id()
    assert len(new_host_id()) >= 8


def test_is_safe_project_name():
    from hosts import project_name_reject_reason
    assert is_safe_project_name('foo')
    assert is_safe_project_name('my-project')
    assert not is_safe_project_name('a/b')
    assert not is_safe_project_name('.hidden')
    assert not is_safe_project_name('')
    assert not is_safe_project_name('$(whoami)')
    assert not is_safe_project_name('x;y')
    assert not is_safe_project_name('a`b')
    assert project_name_reject_reason('') == 'Name required'
    assert project_name_reject_reason('a/b') == 'Name cannot contain /'
    assert 'invalid' in (project_name_reject_reason('$(x)') or '').lower()
    assert project_name_reject_reason('good-name') is None


# ── Settings migration ────────────────────────────────────────────────────────

def test_settings_load_normalizes_hosts_and_overrides(tmp_path):
    path = str(tmp_path / 'settings.json')
    data = {
        'hosts': {
            'h1': {
                'id': 'h1',
                'ssh_target': 'user@host',
                'display_name': 'Lab',
                'rich_status_opt_in': False,
            },
            'junk': {'no': 'target'},
        },
        'harness_overrides': {'/proj/a': 'grok'},
        'provider_overrides': {'/proj/a': 'ollama'},
        'model_pins': {'/proj/a': 'qwen'},
        'remote_health_interval_sec': 15,
    }
    with open(path, 'w') as f:
        json.dump(data, f)
    s = Settings.load(path)
    assert 'h1' in s.hosts
    assert 'junk' not in s.hosts
    assert s.hosts['h1']['ssh_target'] == 'user@host'
    assert s.harness_overrides.get('local:/proj/a') == 'grok'
    assert s.effective_harness('/proj/a') == 'grok'
    # ollama not in providers → stale pin falls back to model_default ('')
    assert s.effective_provider('/proj/a') == ''
    assert s.provider_overrides.get('local:/proj/a') == 'ollama'
    assert s.effective_model('/proj/a') == 'qwen'
    assert s.remote_health_interval_sec == 15
    assert s.section_expanded(LOCALHOST_ID) is True


def test_settings_health_interval_garbage_defaults(tmp_path):
    path = str(tmp_path / 'settings.json')
    with open(path, 'w') as f:
        json.dump({'remote_health_interval_sec': 'nope'}, f)
    s = Settings.load(path)
    assert s.remote_health_interval_sec == 30


def test_settings_host_profiles_roundtrip(tmp_path):
    path = str(tmp_path / 'settings.json')
    s = Settings()
    s.hosts = {
        'abc': HostProfile(
            id='abc', ssh_target='pmhost.example.com', display_name='',
        ).to_dict(),
    }
    s.save(path)
    s2 = Settings.load(path)
    profiles = s2.host_profiles()
    assert 'abc' in profiles
    assert profiles['abc'].title() == 'pmhost.example.com'


def test_effective_harness_remote_ref():
    s = Settings()
    s.harness_overrides = {'ssh:h1:foo': 'opencode'}
    assert s.effective_harness('foo', host_id='h1') == 'opencode'
    assert s.effective_harness('/local/foo') == 'claude'  # default


# ── Session v3 hosts ──────────────────────────────────────────────────────────

def test_session_v3_hosts_roundtrip(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(
        path, ['/a', 'ssh-key'], '/a',
        harnesses={'/a': 'claude', 'ssh-key': 'grok'},
        hosts={'/a': 'localhost', 'ssh-key': 'h1'},
    )
    paths, focused = load_session(path)
    assert paths == ['/a', 'ssh-key']
    assert focused == '/a'
    assert load_harnesses(path)['ssh-key'] == 'grok'
    assert load_hosts(path) == {'/a': 'localhost', 'ssh-key': 'h1'}


def test_session_v2_defaults_host_localhost(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], '/a', harnesses={'/a': 'claude'})
    assert load_hosts(path) == {'/a': 'localhost'}


def test_session_v1_defaults_host_localhost(tmp_path):
    path = str(tmp_path / 'session.json')
    save_session(path, ['/a'], '/a')  # plain strings
    assert load_hosts(path) == {'/a': 'localhost'}


# ── Project.project_ref ───────────────────────────────────────────────────────

def test_project_ref_local_and_remote():
    p = Project(name='foo', path='/home/u/projects/foo')
    assert p.project_ref == 'local:/home/u/projects/foo'
    p2 = Project(name='foo', path='/home/remote/projects/foo', host_id='h1')
    assert p2.project_ref == 'ssh:h1:foo'
