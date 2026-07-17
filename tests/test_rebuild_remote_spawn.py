"""SSH remote argv rewrite — preserve resume flags + model pairs (M4)."""
import harnesses


def test_claude_resume_preserved():
    out = harnesses.rebuild_remote_spawn_argv(
        ['/local/bin/claude', '--resume', 'sess-1'],
        remote_bin='claude',
        adapter_id='claude',
    )
    assert out == ['claude', '--resume', 'sess-1']


def test_kimi_resume_dash_S_preserved_with_model():
    out = harnesses.rebuild_remote_spawn_argv(
        ['/opt/kimi', '-m', 'kimi-code/k3', '-S', 'abc123'],
        remote_bin='kimi',
        adapter_id='kimi',
        continue_falls_back_to_fresh=False,
    )
    assert out == ['kimi', '-m', 'kimi-code/k3', '-S', 'abc123']


def test_opencode_resume_dash_s_preserved():
    out = harnesses.rebuild_remote_spawn_argv(
        ['opencode', '-m', 'ollama/qwen', '-s', 'sid'],
        remote_bin='opencode',
        adapter_id='opencode',
    )
    assert out == ['opencode', '-m', 'ollama/qwen', '-s', 'sid']


def test_grok_resume_dash_r_with_cwd():
    out = harnesses.rebuild_remote_spawn_argv(
        ['grok', '-m', 'pool-qwen', '-r', 'rid'],
        remote_bin='grok',
        adapter_id='grok',
    )
    assert out == ['grok', '--cwd', '.', '-m', 'pool-qwen', '-r', 'rid']


def test_kimi_continue_no_fallback_preserves_model():
    local = harnesses.build_continue_wrapper(
        ['kimi', '-m', 'k3', '-c'],
        ['kimi', '-m', 'k3'],
        fallback=False,
    )
    out = harnesses.rebuild_remote_spawn_argv(
        local,
        remote_bin='kimi',
        adapter_id='kimi',
        continue_falls_back_to_fresh=False,
    )
    assert out[0] == 'bash'
    script = out[-1]
    assert 'kimi -m k3 -c' in script
    # No fresh fallback for kimi.
    assert 's=$?' not in script
    assert 'exec kimi -m k3' in script or "exec kimi -m k3 -c" in script


def test_grok_continue_fallback_preserves_model_and_cwd():
    local = harnesses.build_continue_wrapper(
        ['grok', '-m', 'pool', '-c'],
        ['grok', '-m', 'pool'],
        fallback=True,
    )
    out = harnesses.rebuild_remote_spawn_argv(
        local,
        remote_bin='grok',
        adapter_id='grok',
        continue_falls_back_to_fresh=True,
    )
    assert out[0] == 'bash'
    script = out[-1]
    assert 'grok --cwd . -m pool -c' in script
    assert 'exec grok --cwd . -m pool' in script


def test_fresh_with_model():
    out = harnesses.rebuild_remote_spawn_argv(
        ['/home/x/.local/bin/kimi', '-m', 'k3'],
        remote_bin='kimi',
        adapter_id='kimi',
    )
    assert out == ['kimi', '-m', 'k3']


def test_fresh_bare():
    out = harnesses.rebuild_remote_spawn_argv(
        ['claude'],
        remote_bin='claude',
        adapter_id='claude',
    )
    assert out == ['claude']


def test_bare_continue_flag():
    out = harnesses.rebuild_remote_spawn_argv(
        ['kimi', '-c'],
        remote_bin='kimi',
        adapter_id='kimi',
        continue_falls_back_to_fresh=False,
    )
    assert out[0] == 'bash'
    assert 'kimi -c' in out[-1]
