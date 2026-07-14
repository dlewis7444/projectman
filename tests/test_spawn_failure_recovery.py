"""Spawn-failure recovery blurbs (harnesses.build_spawn_failure_recovery)."""
from harnesses import build_spawn_failure_recovery, harness_install_command


def test_harness_install_command_grok():
    assert harness_install_command('grok') == (
        'curl -fsSL https://x.ai/cli/install.sh | bash'
    )


def test_remote_grok_recovery_customizes_host_and_command():
    rec = build_spawn_failure_recovery(
        'grok',
        binary='grok',
        host_ssh_target='dev@vm.example',
    )
    assert rec.dialog_title == 'Grok Build not installed'
    assert 'dev@vm.example' in rec.dialog_body
    assert rec.is_remote is True
    assert rec.install_command_blurb == harness_install_command('grok')
    assert 'remote host dev@vm.example' in rec.ai_prompt_blurb
    assert harness_install_command('grok') in rec.ai_prompt_blurb
    assert 'verify this is still the current official install command' in rec.ai_prompt_blurb
    assert 'SSH to dev@vm.example' in rec.ai_prompt_blurb


def test_local_claude_recovery_uses_localhost_wording():
    rec = build_spawn_failure_recovery('claude', binary='claude')
    assert rec.is_remote is False
    assert 'this machine' in rec.dialog_body
    assert 'localhost' in rec.ai_prompt_blurb
    assert harness_install_command('claude') in rec.install_command_blurb
    assert 'Run the install command in a terminal' in rec.ai_prompt_blurb