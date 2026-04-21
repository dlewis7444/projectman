import os
import json
import subprocess
from paa_ledger import LedgerItem, make_item_id, now_iso

_HAIKU_TIMEOUT = 30
_MAX_CONTENT_CHARS = 16000

# Manifest files to check for dependency analysis (first found wins)
_MANIFEST_FILES = [
    'requirements.txt', 'pyproject.toml', 'package.json',
    'Cargo.toml', 'go.mod', 'Gemfile', 'pom.xml',
]


def _run_haiku(prompt, settings, timeout=_HAIKU_TIMEOUT):
    """Invoke claude -p --model haiku --output-format json.
    Returns (response_text, tokens_used) or (None, 0) on failure.
    tokens_used = input_tokens + output_tokens (excludes cache)."""
    claude_cmd = settings.resolved_claude_binary
    # Run from PAA's own directory to avoid polluting real project sessions
    paa_dir = os.path.join(settings.resolved_projects_dir, '.project-admin-agent')
    os.makedirs(paa_dir, exist_ok=True)
    try:
        result = subprocess.run(
            [claude_cmd, '-p', '--model', settings.paa_scan_model, '--output-format', 'json', prompt],
            capture_output=True, text=True, timeout=timeout,
            cwd=paa_dir, stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return (None, 0)
    if result.returncode != 0:
        return (None, 0)
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return (None, 0)
    response_text = data.get('result', '')
    usage = data.get('usage', {})
    tokens = usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
    return (response_text, tokens)


def _parse_haiku_response(text):
    """Parse structured JSON response from Haiku.
    Returns list of dicts with 'summary' and 'evidence' keys, or [] on failure."""
    # Strip markdown code fences (Haiku often wraps JSON in ```json ... ```)
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else ''
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()
    try:
        data = json.loads(text)
        issues = data.get('issues', [])
        if not isinstance(issues, list):
            return []
        return [i for i in issues if isinstance(i, dict) and 'summary' in i]
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def _read_truncated(path, max_chars=_MAX_CONTENT_CHARS):
    """Read file content, truncated to max_chars."""
    try:
        with open(path, 'r') as f:
            return f.read(max_chars)
    except (OSError, UnicodeDecodeError):
        return None


def _top_level_listing(project_path):
    """Get top-level directory listing."""
    try:
        entries = sorted(e.name for e in os.scandir(project_path)
                        if e.name != '.git')
        return '\n'.join(entries)
    except OSError:
        return ''


def check_semantic_staleness(project_name, project_path, settings):
    """AI check: is CLAUDE.md still accurate for the codebase?
    Returns (list[LedgerItem], int tokens_used)."""
    claude_md = os.path.join(project_path, 'CLAUDE.md')
    content = _read_truncated(claude_md)
    if content is None:
        return ([], 0)

    listing = _top_level_listing(project_path)
    prompt = (
        'You are auditing a project\'s CLAUDE.md for accuracy.\n'
        'IMPORTANT: Do NOT read any files yourself. ALL project data is provided below. '
        'Your working directory is NOT the project — do not inspect it.\n\n'
        f'Project: {project_name}\n'
        f'Project directory listing (top-level):\n{listing}\n\n'
        f'CLAUDE.md contents (may be truncated):\n{content}\n\n'
        'Check if CLAUDE.md references files, directories, commands, or patterns '
        'that no longer match the actual project structure shown in the listing above.\n'
        'Only flag concrete mismatches where a referenced file/directory is NOT in the listing. '
        'Do NOT flag truncation, incomplete sections, or markdown formatting.\n\n'
        'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
        'Set "critical" to true ONLY for issues that would cause builds to fail, '
        'data loss, or security vulnerabilities. Stale documentation alone is never critical.\n'
        'If CLAUDE.md accurately reflects the project, respond: {"issues": []}'
    )
    response, tokens = _run_haiku(prompt, settings)
    if response is None:
        return ([], 0)

    issues = _parse_haiku_response(response)
    items = []
    for issue in issues:
        summary = issue.get('summary', 'Semantic staleness detected')
        evidence = issue.get('evidence', '')
        severity = 'critical' if issue.get('critical') else 'warning'
        item_id = make_item_id('ai-semantic-staleness', project_name, summary)
        items.append(LedgerItem(
            id=item_id,
            type='ai-semantic-staleness',
            project=project_name,
            project_path=project_path,
            summary=summary,
            evidence=evidence,
            severity=severity,
            created=now_iso(),
        ))
    return (items, tokens)


def check_dependency_versions(project_name, project_path, settings):
    """AI check: are there outdated/problematic dependencies?
    Returns (list[LedgerItem], int tokens_used)."""
    manifest_content = None
    manifest_name = None
    for fname in _MANIFEST_FILES:
        content = _read_truncated(os.path.join(project_path, fname))
        if content is not None:
            manifest_content = content
            manifest_name = fname
            break
    if manifest_content is None:
        return ([], 0)

    prompt = (
        f'Check this {manifest_name} for outdated, insecure, or problematic dependencies.\n'
        'IMPORTANT: Do NOT read any files yourself. ALL data is provided below.\n\n'
        f'{manifest_name}:\n{manifest_content}\n\n'
        'Only flag dependencies that are significantly outdated (major version behind) '
        'or have known security issues. Do not flag minor version differences.\n\n'
        'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
        'Set "critical" to true ONLY for dependencies with known CVEs or security advisories. '
        'Being outdated alone is never critical.\n'
        'If no significant issues: {"issues": []}'
    )
    response, tokens = _run_haiku(prompt, settings)
    if response is None:
        return ([], 0)

    issues = _parse_haiku_response(response)
    items = []
    for issue in issues:
        summary = issue.get('summary', 'Dependency issue detected')
        evidence = issue.get('evidence', '')
        severity = 'critical' if issue.get('critical') else 'info'
        item_id = make_item_id('ai-dependency-outdated', project_name, summary)
        items.append(LedgerItem(
            id=item_id,
            type='ai-dependency-outdated',
            project=project_name,
            project_path=project_path,
            summary=summary,
            evidence=evidence,
            severity=severity,
            created=now_iso(),
        ))
    return (items, tokens)


def check_project_health(project_name, project_path, settings):
    """AI check: general project health scan.
    Returns (list[LedgerItem], int tokens_used)."""
    listing = _top_level_listing(project_path) or '(no visible files)'

    context = listing
    readme = _read_truncated(os.path.join(project_path, 'README.md'))
    if readme:
        context += f'\n\nREADME.md (truncated):\n{readme}'
    claude_md = _read_truncated(os.path.join(project_path, 'CLAUDE.md'))
    if claude_md:
        context += f'\n\nCLAUDE.md (truncated):\n{claude_md}'

    prompt = (
        'You are doing a quick health check on a project.\n'
        'IMPORTANT: Do NOT read any files yourself. ALL project data is provided below. '
        'Your working directory is NOT the project — do not inspect it.\n'
        'The file listing below is COMPLETE and AUTHORITATIVE — if a file appears, it EXISTS.\n\n'
        f'Project: {project_name}\n'
        f'Contents:\n{context}\n\n'
        'Assume this is an internal/private project unless the README explicitly '
        'says it is published, distributed externally, or open-source.\n\n'
        'NEVER flag any of the following, even if the project appears to "lack" them. '
        'The default assumption is that their absence is intentional:\n'
        '  - Missing LICENSE, COPYING, NOTICE, or any licensing file\n'
        '  - Missing README.md (CLAUDE.md often serves as the primary doc)\n'
        '  - Missing .gitignore — UNLESS the listing clearly shows committed files '
        'that obviously should be ignored (e.g., a tracked `.env`, a `node_modules/` '
        'directory, large build artifacts checked in by mistake)\n'
        '  - Missing CLAUDE.md (handled separately)\n'
        '  - Missing tests, CI config, docs/, or contributing guides\n'
        '  - Empty directory / "project appears uninitialized" — empty is valid state\n'
        '  - Screenshots, images, or files at the root rather than in a subdir\n'
        '  - File organization, naming conventions, or stylistic preferences\n'
        '  - Duplicate or repeated content across files, UNLESS the duplication '
        'is clearly dangerous (e.g., two different sets of credentials, or two '
        'copies of a value that must stay in sync with no mechanism to enforce it)\n'
        '  - Files that ARE present in the listing (read carefully before flagging)\n\n'
        'DO flag only concrete, actionable problems you are highly confident about:\n'
        '  - Secrets or credentials committed in plaintext\n'
        '  - Broken internal references (README/CLAUDE.md points at a file NOT in the listing)\n'
        '  - Obvious security risks (world-writable configs, hard-coded tokens in scripts)\n'
        '  - Clear contradictions between claimed and observed state\n\n'
        'When in doubt, do not flag. False positives are more costly than missed nits.\n\n'
        'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
        'Set "critical" to true ONLY for security risks, data loss risks, '
        'or broken deployments. Missing best-practice files are never critical.\n'
        'If project looks healthy: {"issues": []}'
    )
    response, tokens = _run_haiku(prompt, settings)
    if response is None:
        return ([], 0)

    issues = _parse_haiku_response(response)
    items = []
    for issue in issues:
        summary = issue.get('summary', 'Health concern detected')
        evidence = issue.get('evidence', '')
        severity = 'critical' if issue.get('critical') else 'info'
        item_id = make_item_id('ai-health-concern', project_name, summary)
        items.append(LedgerItem(
            id=item_id,
            type='ai-health-concern',
            project=project_name,
            project_path=project_path,
            summary=summary,
            evidence=evidence,
            severity=severity,
            created=now_iso(),
        ))
    return (items, tokens)


def run_ai_checks(project_name, project_path, settings):
    """Run all AI checks for one project. Respects paa_allow_haiku.
    Returns (list[LedgerItem], int total_tokens_used)."""
    if not settings.paa_allow_haiku:
        return ([], 0)
    items = []
    total_tokens = 0
    for check_fn in [check_semantic_staleness, check_dependency_versions, check_project_health]:
        try:
            new_items, tokens = check_fn(project_name, project_path, settings)
            items.extend(new_items)
            total_tokens += tokens
        except Exception:
            continue  # Don't let one check failure block others
    return (items, total_tokens)
