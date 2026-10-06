import contextvars
import logging
import os
from contextlib import contextmanager
from dataclasses import replace

from paa_ledger import LedgerItem, make_item_id, now_iso

_SCAN_TIMEOUT = 30
_MAX_CONTENT_CHARS = 16000

log = logging.getLogger(__name__)

# Log-once window for scan harness fallback messages.
# * None (default): log every resolve / plan-None reason (standalone
#   ``_run_scan_model`` and other callers).
# * ``{'logged': bool}``: at most one ``paa scan harness fallback`` line for
#   the whole ``run_ai_checks`` run (covers resolve *and* plan-None). Using a
#   ContextVar so concurrent ThreadPoolExecutor scans do not share state.
_scan_fallback_log_once = contextvars.ContextVar(
    'paa_scan_fallback_log_once', default=None)


def _maybe_log_scan_fallback(reason):
    """Log a scan harness fallback reason, honoring the once-per-run window."""
    if not reason:
        return
    state = _scan_fallback_log_once.get()
    if state is not None:
        if state.get('logged'):
            return
        state['logged'] = True
    log.info('paa scan harness fallback: %s', reason)


@contextmanager
def _scan_fallback_once_window():
    """At most one fallback log for the duration of the ``with`` block."""
    token = _scan_fallback_log_once.set({'logged': False})
    try:
        yield
    finally:
        _scan_fallback_log_once.reset(token)

# Manifest files to check for dependency analysis (first found wins)
_MANIFEST_FILES = [
    'requirements.txt', 'pyproject.toml', 'package.json',
    'Cargo.toml', 'go.mod', 'Gemfile', 'pom.xml',
]


def _claude_scan_model(settings, project_path):
    """Resolve the claude-axis scan tier (``paa_scan_model``).

    Returns the tier alias string (e.g. ``'haiku'``). Provider-side tier→model
    id resolution happens inside ``ClaudeAdapter.headless_plan`` when a custom
    provider is active. Tier aliases apply only when the resolved harness is
    claude (callers pass this model only on that path).
    """
    tier = (settings.paa_scan_model or 'haiku')
    if isinstance(tier, str):
        tier = tier.strip() or 'haiku'
    else:
        tier = 'haiku'
    return tier


def _run_scan_model(prompt, settings, project_path=None, timeout=_SCAN_TIMEOUT):
    """Invoke the effective harness headlessly for an AI scan.

    Routes through ``paa_headless.resolve_headless_adapter`` (same harness axis
    as the main UI) and ``adapter.headless_plan`` / ``run_headless``. Claude
    tier aliases (``paa_scan_model``) apply only when the resolved adapter is
    claude; non-claude adapters receive ``model=None`` (their normal default).

    *project_path* is optional for back-compat tests; production callers must
    pass it (use ``''`` for global default provider — no project pin).

    Fallback logging uses ``_maybe_log_scan_fallback`` so ``run_ai_checks`` can
    log once per scan run for resolve *or* plan-None reasons (not 0 for
    plan-None, not 3× across checks).

    Returns ``(response_text, tokens_used)`` or ``(None, 0)`` on failure.
    tokens_used = input_tokens + output_tokens (excludes cache).

    Failure semantics are preserved: any error → ``(None, 0)``.
    """
    from paa_headless import (
        plan_with_none_fallback,
        resolve_headless_adapter,
        run_headless,
    )

    # Back-compat: project_path=None means "native inherit, no provider axis".
    # Resolution still uses '' for the harness axis (global default).
    resolve_path = '' if project_path is None else project_path
    adapter, hid, fallback_reason = resolve_headless_adapter(
        settings, resolve_path)
    _maybe_log_scan_fallback(fallback_reason)
    if adapter is None:
        return (None, 0)

    # Claude-axis tier only when the adapter that will actually run is claude.
    scan_tier = _claude_scan_model(settings, project_path)
    if getattr(adapter, 'id', None) == 'claude':
        model = scan_tier
    else:
        model = None

    # If headless_plan returns None, fall back to claude (W5 safety) rather
    # than silent (None, 0) with no claude attempt.
    adapter, plan, none_reason = plan_with_none_fallback(
        adapter, hid, settings, prompt, project_path,
        session_id=None, model=model, claude_model=scan_tier,
    )
    _maybe_log_scan_fallback(none_reason)
    if plan is None or adapter is None:
        return (None, 0)

    # Explicit scan-routing line (debug only). run_headless also logs the
    # full argv; this names the resolved harness even when argv is opaque.
    try:
        from debug_log import debug_log
        aid = getattr(adapter, 'id', hid)
        reason = none_reason or fallback_reason
        msg = f'paa scan harness={aid} project_path={resolve_path!r}'
        if reason:
            msg += f' fallback={reason}'
        debug_log(settings, msg)
    except Exception:
        pass

    # Prefer a fresh plan with the requested timeout over mutating the
    # adapter-returned dataclass in place.
    if plan.timeout != timeout:
        plan = replace(plan, timeout=timeout)

    result = run_headless(
        plan, adapter.parse_headless_output, settings=settings)
    if result.text is None:
        return (None, 0)
    return (result.text, result.tokens)


# Back-compat alias (older imports / tests may still use the haiku name).
_run_haiku = _run_scan_model


def _parse_haiku_response(text):
    """Parse structured JSON response from Haiku.
    Returns list of dicts with 'summary' and 'evidence' keys, or [] on failure."""
    import json
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
    """AI check: is AGENTS.md still accurate for the codebase?
    Returns (list[LedgerItem], int tokens_used)."""
    agents_md = os.path.join(project_path, 'AGENTS.md')
    claude_md = os.path.join(project_path, 'CLAUDE.md')
    rules_file = 'AGENTS.md'
    content = _read_truncated(agents_md)
    if content is None:
        content = _read_truncated(claude_md)
        rules_file = 'CLAUDE.md'
    if content is None:
        return ([], 0)

    listing = _top_level_listing(project_path)
    prompt = (
        f'You are auditing a project\'s {rules_file} for accuracy.\n'
        'IMPORTANT: Do NOT read any files yourself. ALL project data is provided below. '
        'Your working directory is NOT the project — do not inspect it.\n\n'
        f'Project: {project_name}\n'
        f'Project directory listing (top-level):\n{listing}\n\n'
        f'{rules_file} contents (may be truncated):\n{content}\n\n'
        f'Check if {rules_file} references files, directories, commands, or patterns '
        'that no longer match the actual project structure shown in the listing above.\n'
        'Only flag concrete mismatches where a referenced file/directory is NOT in the listing. '
        'Do NOT flag truncation, incomplete sections, or markdown formatting.\n\n'
        'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
        'Set "critical" to true ONLY for issues that would cause builds to fail, '
        'data loss, or security vulnerabilities. Stale documentation alone is never critical.\n'
        f'If {rules_file} accurately reflects the project, respond: {{"issues": []}}'
    )
    response, tokens = _run_scan_model(prompt, settings, project_path=project_path)
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
    response, tokens = _run_scan_model(prompt, settings, project_path=project_path)
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
    agents_md = _read_truncated(os.path.join(project_path, 'AGENTS.md'))
    rules_file = 'AGENTS.md'
    if agents_md is None:
        agents_md = _read_truncated(os.path.join(project_path, 'CLAUDE.md'))
        rules_file = 'CLAUDE.md'
    if agents_md:
        context += f'\n\n{rules_file} (truncated):\n{agents_md}'

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
        '  - Missing README.md (AGENTS.md often serves as the primary doc)\n'
        '  - Missing .gitignore — UNLESS the listing clearly shows committed files '
        'that obviously should be ignored (e.g., a tracked `.env`, a `node_modules/` '
        'directory, large build artifacts checked in by mistake)\n'
        '  - Missing AGENTS.md (handled separately)\n'
        '  - Missing tests, CI config, docs/, or contributing guides\n'
        '  - Empty directory / "project appears uninitialized" — empty is valid state\n'
        '  - Screenshots, images, or files at the root rather than in a subdir\n'
        '  - File organization, naming conventions, or stylistic preferences\n'
        '  - Duplicate or repeated content across files, UNLESS the duplication '
        'is clearly dangerous (e.g., two different sets of credentials, or two '
        'copies of a value that must stay in sync with no mechanism to enforce it)\n'
        '  - Files that ARE present in the listing (read carefully before flagging)\n\n'
        'DO flag only concrete, actionable problems you are highly confident about:\n'
        f'  - Secrets or credentials committed in plaintext\n'
        f'  - Broken internal references (README/{rules_file} points at a file NOT in the listing)\n'
        '  - Obvious security risks (world-writable configs, hard-coded tokens in scripts)\n'
        '  - Clear contradictions between claimed and observed state\n\n'
        'When in doubt, do not flag. False positives are more costly than missed nits.\n\n'
        'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
        'Set "critical" to true ONLY for security risks, data loss risks, '
        'or broken deployments. Missing best-practice files are never critical.\n'
        'If project looks healthy: {"issues": []}'
    )
    response, tokens = _run_scan_model(prompt, settings, project_path=project_path)
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
    Returns (list[LedgerItem], int total_tokens_used).

    Logs harness fallback **once per scan run** for resolve *or* plan-None
    reasons (not once per check, and not silent when only plan-None fires).
    No UI surface in Phase A.
    """
    if not settings.paa_allow_haiku:
        return ([], 0)

    items = []
    total_tokens = 0
    # Log-once window: first resolve reason or plan-None none_reason from any
    # of the three checks emits one line; later checks stay quiet.
    with _scan_fallback_once_window():
        for check_fn in [
            check_semantic_staleness,
            check_dependency_versions,
            check_project_health,
        ]:
            try:
                new_items, tokens = check_fn(
                    project_name, project_path, settings)
                items.extend(new_items)
                total_tokens += tokens
            except Exception:
                continue  # Don't let one check failure block others
    return (items, total_tokens)
