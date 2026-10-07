"""GTK-free tests for PAA Chat/Discuss harness resolution.

PAA interactive spawn must follow ``settings.effective_harness('')`` (global
default), not hardcode ``claude``. Claude keeps the PAA chat-tier overlay.
"""
from pathlib import Path

from settings import Settings
from paa_deploy import paa_interactive_plan, write_startup_prompt


def _provider(pid='ollama', base_url='http://localhost:11434', api_key='k',
              models=None):
    return {
        pid: {
            'name': 'Ollama',
            'base_url': base_url,
            'api_key': api_key,
            'models': models if models is not None else ['glm-5.2:cloud[1m]'],
        }
    }


def test_interactive_plan_grok_default_is_not_claude(tmp_path):
    settings = Settings(harness_default='grok')
    plan = paa_interactive_plan(settings, str(tmp_path), 'WELCOME')
    assert plan.harness_id == 'grok'
    assert plan.plan.argv[0] == 'grok'
    assert 'claude' not in plan.plan.argv[0]
    assert plan.plan.env is None
    # Non-claude: prompt is not a trailing interactive argv (that's -p / headless).
    assert 'WELCOME' not in plan.plan.argv


def test_interactive_plan_kimi_and_opencode(tmp_path):
    for hid in ('kimi', 'opencode'):
        settings = Settings(harness_default=hid)
        plan = paa_interactive_plan(settings, str(tmp_path), 'WELCOME')
        assert plan.harness_id == hid
        assert plan.plan.argv[0] == hid
        assert plan.plan.env is None


def test_interactive_plan_claude_native_passes_tier_and_prompt(tmp_path):
    settings = Settings(harness_default='claude', paa_chat_model='haiku')
    plan = paa_interactive_plan(settings, str(tmp_path), 'WELCOME')
    assert plan.harness_id == 'claude'
    argv = plan.plan.argv
    assert argv[0] == 'claude'
    assert '--model' in argv
    assert argv[argv.index('--model') + 1] == 'haiku'
    assert argv[-1] == 'WELCOME'
    assert plan.plan.env is None


def test_interactive_plan_claude_custom_provider_resolves_tier(tmp_path):
    settings = Settings(
        harness_default='claude',
        paa_chat_model='sonnet',
        provider_defaults={'claude': 'ollama'},
        providers=_provider(models=['fast-id', 'std-id']),
        tier_models={'ollama': {'sonnet': 'std-id', 'haiku': 'fast-id',
                                'opus': 'std-id'}},
    )
    plan = paa_interactive_plan(settings, str(tmp_path), 'WELCOME')
    assert plan.harness_id == 'claude'
    assert plan.plan.env is not None
    assert plan.plan.env['ANTHROPIC_BASE_URL'] == 'http://localhost:11434'
    argv = plan.plan.argv
    assert argv[argv.index('--model') + 1] == 'std-id'
    assert argv[-1] == 'WELCOME'


def test_write_startup_prompt(tmp_path):
    paa_dir = tmp_path / '.project-admin-agent'
    paa_dir.mkdir()
    path = write_startup_prompt(str(paa_dir), 'WELCOME')
    assert Path(path).read_text() == 'WELCOME\n'
