"""PAA package deploy + interactive Discuss/chat spawn plan.

The chat/Discuss VTE path used to ``shutil.copy2`` ``paa/CLAUDE.md`` and
raise uncaught ``FileNotFoundError`` after the AGENTS.md migration. Deploy
now requires ``AGENTS.md`` (optional read of a leftover package
``CLAUDE.md``) and writes **only** AGENTS names.

Interactive chat/Discuss resolves the global default harness
(``settings.effective_harness('')``) the same way the main UI does. Failures
must surface in the card window — never a silent GTK traceback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from harnesses import SpawnPlan, get_adapter


RULES_NAME = 'AGENTS.md'
SUPPLEMENT_NAME = 'AGENTS-SUPPLEMENT.md'
_LEGACY_RULES = 'CLAUDE.md'
_LEGACY_SUPPLEMENT = 'CLAUDE-SUPPLEMENT.md'

_CLAUDE_TIER_ALIASES = frozenset(('haiku', 'sonnet', 'opus', 'fable', 'subagent'))

_USER_MD_STUB = (
    '<!-- Custom instructions for the Projects Admin Agent. -->\n'
    '<!-- This file is yours — ProjectMan will never overwrite it. -->\n'
)


class PAAHarnessError(FileNotFoundError):
    """PAA package is missing a file the chat/Discuss spawn needs."""


@dataclass
class PAAInteractivePlan:
    """Resolved interactive spawn: harness id + adapter ``SpawnPlan``."""
    harness_id: str
    plan: SpawnPlan


class _PaaProject:
    """Minimal project stand-in so adapters can call ``spawn_plan``."""

    def __init__(self, path):
        self.path = path
        self.host_id = 'localhost'
        self.name = '.project-admin-agent'


def _first_existing(src_dir, names):
    for name in names:
        path = os.path.join(src_dir, name)
        if os.path.isfile(path):
            return path
    return None


def resolve_paa_package_files(src_dir):
    """Locate package files. Raises ``PAAHarnessError`` if a required file is missing.

    AGENTS.md is required in current packages. A leftover ``CLAUDE.md`` in an
    old install tree is accepted as a *read* fallback only.
    """
    rules_src = _first_existing(src_dir, (RULES_NAME, _LEGACY_RULES))
    if rules_src is None:
        raise PAAHarnessError(
            f'PAA package is missing {RULES_NAME} in {src_dir}'
        )
    supplement_src = _first_existing(
        src_dir, (SUPPLEMENT_NAME, _LEGACY_SUPPLEMENT))
    if supplement_src is None:
        raise PAAHarnessError(
            f'PAA package is missing {SUPPLEMENT_NAME} in {src_dir}'
        )
    gather_src = os.path.join(src_dir, 'gather-context.sh')
    if not os.path.isfile(gather_src):
        raise PAAHarnessError(
            f'PAA package is missing gather-context.sh in {src_dir}'
        )
    settings_src = os.path.join(src_dir, 'settings.json')
    if not os.path.isfile(settings_src):
        raise PAAHarnessError(
            f'PAA package is missing settings.json in {src_dir}'
        )
    return {
        'rules': rules_src,
        'supplement': supplement_src,
        'gather': gather_src,
        'settings': settings_src,
    }


def deploy_paa_harness(src_dir, projects_dir):
    """Install harness files under ``projects_dir/.project-admin-agent``.

    Writes AGENTS.md and AGENTS-SUPPLEMENT.md only (no CLAUDE.md dual-write).
    Creates ``USER.md`` once. Refreshes ``project-snapshot.md`` best-effort.
    Returns the harness directory path.
    """
    files = resolve_paa_package_files(src_dir)
    paa_dir = os.path.join(projects_dir, '.project-admin-agent')
    system_dir = os.path.join(paa_dir, '.system')
    claude_dir = os.path.join(paa_dir, '.claude')
    os.makedirs(system_dir, exist_ok=True)
    os.makedirs(claude_dir, exist_ok=True)

    shutil.copy2(files['rules'], os.path.join(paa_dir, RULES_NAME))
    shutil.copy2(files['supplement'], os.path.join(system_dir, SUPPLEMENT_NAME))
    shutil.copy2(files['settings'], os.path.join(claude_dir, 'settings.json'))

    gather_dst = os.path.join(system_dir, 'gather-context.sh')
    shutil.copy2(files['gather'], gather_dst)
    os.chmod(gather_dst, 0o755)

    user_md = os.path.join(paa_dir, 'USER.md')
    if not os.path.exists(user_md):
        with open(user_md, 'w') as f:
            f.write(_USER_MD_STUB)

    try:
        subprocess.run(
            [gather_dst], cwd=system_dir,
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    return paa_dir


def write_startup_prompt(paa_dir, prompt):
    """Write ``.system/startup-prompt.md`` for harness-neutral On Startup."""
    system_dir = os.path.join(paa_dir, '.system')
    os.makedirs(system_dir, exist_ok=True)
    path = os.path.join(system_dir, 'startup-prompt.md')
    text = prompt if prompt.endswith('\n') else prompt + '\n'
    with open(path, 'w') as f:
        f.write(text)
    return path


def paa_interactive_plan(settings, paa_dir, prompt):
    """Build the interactive Chat/Discuss spawn for the default harness.

    Uses ``effective_harness('')`` (no project pin). Claude overlay: PAA chat
    tier as ``--model`` plus the trailing prompt (WELCOME / DISCUSS FINDING).
    Other harnesses get a plain ``fresh`` spawn; the prompt lives in
    ``.system/startup-prompt.md``.
    """
    requested = settings.effective_harness('')
    adapter = get_adapter(requested, settings)
    hid = getattr(adapter, 'id', requested) or requested
    project = _PaaProject(paa_dir)
    plan = adapter.spawn_plan(settings, project, 'fresh')
    argv = list(plan.argv)
    if hid == 'claude' and prompt:
        from models import resolve_tier_model
        tier = (getattr(settings, 'paa_chat_model', None) or 'sonnet').strip() or 'sonnet'
        model = tier
        if plan.env is not None and tier in _CLAUDE_TIER_ALIASES:
            resolved = resolve_tier_model(
                settings, settings.effective_provider('', 'claude'), tier)
            if resolved:
                model = resolved
        if argv:
            argv = list(argv) + ['--model', model, prompt]
        else:
            argv = [adapter._binary(settings), '--model', model, prompt]
    return PAAInteractivePlan(
        harness_id=hid,
        plan=SpawnPlan(
            argv=argv,
            env=plan.env,
            fallback_reason=plan.fallback_reason,
        ),
    )
