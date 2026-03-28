"""Cross-project checks for PAA Phase 4.

These checks analyse the full project set collectively, not per-project.
They produce LedgerItems with type prefix 'xp-'.
"""

import json
import os
import re
import subprocess
import time

from paa_ledger import LedgerItem, make_item_id, now_iso

_MANIFEST_FILES = [
    'requirements.txt', 'pyproject.toml', 'package.json',
    'Cargo.toml', 'go.mod', 'Gemfile', 'pom.xml',
]


# ── Manifest parsers ──────────────────────────────────────────────────────

def _parse_requirements_txt(path):
    """Parse requirements.txt → {package: version_spec}."""
    deps = {}
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('-'):
                    continue
                m = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9._-]*)\s*(.*)$', line)
                if m:
                    name = m.group(1).lower().replace('_', '-')
                    spec = m.group(2).strip().rstrip(',')
                    deps[name] = spec or '*'
    except OSError:
        pass
    return deps


def _parse_pyproject_toml(path):
    """Parse pyproject.toml dependencies → {package: version_spec}.
    Simple regex parser — extracts from [project] dependencies list."""
    deps = {}
    try:
        with open(path, 'r') as f:
            text = f.read()
    except OSError:
        return deps
    # Find dependencies = [...] in [project] section
    m = re.search(
        r'\[project\].*?dependencies\s*=\s*\[(.*?)\]',
        text, re.DOTALL,
    )
    if not m:
        return deps
    block = m.group(1)
    for dep_match in re.finditer(r'"([^"]+)"', block):
        dep_str = dep_match.group(1)
        pm = re.match(r'^([a-zA-Z0-9_][a-zA-Z0-9._-]*)\s*(.*)$', dep_str)
        if pm:
            name = pm.group(1).lower().replace('_', '-')
            spec = pm.group(2).strip()
            deps[name] = spec or '*'
    return deps


def _parse_package_json(path):
    """Parse package.json → {package: version_spec}."""
    deps = {}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return deps
    for section in ('dependencies', 'devDependencies'):
        section_deps = data.get(section, {})
        if isinstance(section_deps, dict):
            for name, spec in section_deps.items():
                deps[name.lower()] = spec
    return deps


_PARSERS = {
    'requirements.txt': _parse_requirements_txt,
    'pyproject.toml': _parse_pyproject_toml,
    'package.json': _parse_package_json,
}


# ── Check functions ───────────────────────────────────────────────────────

def check_stale_projects(projects, settings):
    """Flag projects with no git commits in > paa_stale_days days."""
    threshold = getattr(settings, 'paa_stale_days', 60) * 86400
    now = time.time()
    items = []
    for project in projects:
        git_dir = os.path.join(project.path, '.git')
        if not os.path.isdir(git_dir):
            continue
        try:
            result = subprocess.run(
                ['git', '-C', project.path, 'log', '-1', '--format=%ct'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            last_commit = int(result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError, ValueError):
            continue
        age = now - last_commit
        if age > threshold:
            days = int(age / 86400)
            from datetime import datetime, timezone
            commit_date = datetime.fromtimestamp(last_commit, tz=timezone.utc)
            date_str = commit_date.strftime('%Y-%m-%d')
            items.append(LedgerItem(
                id=make_item_id('xp-stale-project', project.name, ''),
                type='xp-stale-project',
                project=project.name,
                project_path=project.path,
                summary=f'{project.name} has had no git commits in {days} days',
                evidence=f'Last commit: {date_str} ({days} days ago)',
                severity='info',
                created=now_iso(),
            ))
    return items


def check_cross_references(projects):
    """Check CLAUDE.md files for broken references to sibling projects."""
    known_names = {p.name for p in projects}
    known_paths = {p.path for p in projects}
    items = []
    for project in projects:
        claude_md = os.path.join(project.path, 'CLAUDE.md')
        if not os.path.isfile(claude_md):
            continue
        try:
            with open(claude_md, 'r') as f:
                text = f.read()
        except OSError:
            continue
        # Find ../sibling-project/ references
        for m in re.finditer(r'\.\./([^/\s)]+)', text):
            ref_name = m.group(1)
            ref_path = os.path.normpath(
                os.path.join(project.path, '..', ref_name)
            )
            if ref_name in known_names:
                continue  # valid reference
            if os.path.exists(ref_path):
                continue  # path exists even if not a project
            ref_text = f'../{ref_name}'
            items.append(LedgerItem(
                id=make_item_id('xp-broken-reference', project.name, ref_text),
                type='xp-broken-reference',
                project=project.name,
                project_path=project.path,
                summary=f'{project.name}/CLAUDE.md references missing sibling: {ref_text}',
                evidence=f'Reference to {ref_text} in CLAUDE.md — target not found',
                severity='warning',
                created=now_iso(),
            ))
    return items


def check_shared_dep_conflicts(projects, settings):
    """Find dependency version conflicts across projects."""
    # Phase A: build {package: {project: version_spec}} map
    dep_map = {}  # {package_name: {project_name: version_spec}}
    for project in projects:
        for manifest_name, parser in _PARSERS.items():
            manifest_path = os.path.join(project.path, manifest_name)
            if os.path.isfile(manifest_path):
                deps = parser(manifest_path)
                for pkg, spec in deps.items():
                    dep_map.setdefault(pkg, {})[project.name] = spec
                break  # first manifest wins per project

    # Find conflicts: same package, different version specs
    conflicts = {}
    for pkg, proj_specs in dep_map.items():
        if len(proj_specs) < 2:
            continue
        specs = set(proj_specs.values())
        if len(specs) > 1:
            conflicts[pkg] = proj_specs

    if not conflicts:
        return ([], 0)

    # Build filesystem-based items
    items = []
    for pkg, proj_specs in conflicts.items():
        spec_parts = [f'{proj}: {spec}' for proj, spec in sorted(proj_specs.items())]
        evidence = '; '.join(spec_parts)
        items.append(LedgerItem(
            id=make_item_id('xp-dep-conflict', pkg, evidence),
            type='xp-dep-conflict',
            project='(cross-project)',
            project_path='',
            summary=f'Version conflict for {pkg} across {len(proj_specs)} projects',
            evidence=evidence,
            severity='warning',
            created=now_iso(),
        ))

    # Phase B: AI analysis if budget allows
    tokens = 0
    try:
        from paa_monitor import _budget_allows_ai
        if _budget_allows_ai(settings):
            from paa_haiku import _run_haiku, _parse_haiku_response
            conflict_text = '\n'.join(
                f'- {pkg}: ' + ', '.join(
                    f'{proj}={spec}' for proj, spec in sorted(specs.items())
                )
                for pkg, specs in conflicts.items()
            )
            prompt = (
                'You are reviewing dependency version conflicts across multiple projects.\n\n'
                f'Conflicts found:\n{conflict_text}\n\n'
                'For each conflict, assess:\n'
                '1. Are these versions likely incompatible?\n'
                '2. Are there known security issues with any of the older versions?\n\n'
                'Respond with JSON only: {"issues": [{"summary": "...", "evidence": "...", "critical": false}]}\n'
                'Set "critical" to true ONLY for known CVEs or confirmed incompatibilities.\n'
                'If all conflicts are benign version range differences: {"issues": []}'
            )
            response, tokens = _run_haiku(prompt, settings)
            if response:
                ai_issues = _parse_haiku_response(response)
                for issue in ai_issues:
                    if issue.get('critical'):
                        # Upgrade matching items to critical
                        for item in items:
                            pkg_name = item.summary.split("'")[0].split(' for ')[-1].split(' across')[0]
                            if pkg_name in issue.get('evidence', ''):
                                item.severity = 'critical'
    except Exception:
        pass

    return (items, tokens)


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_cross_project_checks(projects, settings):
    """Run all cross-project checks. Returns (list[LedgerItem], int tokens)."""
    items = []
    total_tokens = 0

    for check_fn in [
        lambda: check_stale_projects(projects, settings),
        lambda: check_cross_references(projects),
    ]:
        try:
            items.extend(check_fn())
        except Exception:
            continue

    try:
        dep_items, tokens = check_shared_dep_conflicts(projects, settings)
        items.extend(dep_items)
        total_tokens += tokens
    except Exception:
        pass

    return (items, total_tokens)
