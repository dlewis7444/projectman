"""Tests for paa_cross_project — Phase 4 cross-project checks."""

import json
import os
import time
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

import pytest

from paa_cross_project import (
    check_stale_projects,
    check_cross_references,
    check_shared_dep_conflicts,
    run_cross_project_checks,
    _parse_requirements_txt,
    _parse_pyproject_toml,
    _parse_package_json,
)


@dataclass
class FakeProject:
    name: str
    path: str
    is_archived: bool = False


@dataclass
class FakeSettings:
    paa_stale_days: int = 60
    paa_allow_haiku: bool = False
    paa_budget_unlimited: bool = False
    paa_budget_used: int = 0
    paa_budget_tokens: int = 100000
    paa_scan_model: str = 'haiku'
    resolved_claude_binary: str = 'claude'
    resolved_projects_dir: str = '/tmp'


def _make_project(tmp_path, name, with_git=True, claude_md=None, manifest=None):
    """Create a project dir and return a FakeProject."""
    proj = tmp_path / name
    proj.mkdir(exist_ok=True)
    if with_git:
        (proj / '.git').mkdir()
    if claude_md:
        (proj / 'CLAUDE.md').write_text(claude_md)
    if manifest:
        for filename, content in manifest.items():
            (proj / filename).write_text(content)
    return FakeProject(name=name, path=str(proj))


# ── Parser tests ──────────────────────────────────────────────────────────

def test_parse_requirements_txt(tmp_path):
    f = tmp_path / 'requirements.txt'
    f.write_text('requests>=2.28\nhttpx==0.27.0\n# comment\n-e .\nflask\n')
    deps = _parse_requirements_txt(str(f))
    assert deps['requests'] == '>=2.28'
    assert deps['httpx'] == '==0.27.0'
    assert deps['flask'] == '*'
    assert '-e' not in deps


def test_parse_pyproject_toml(tmp_path):
    f = tmp_path / 'pyproject.toml'
    f.write_text('''
[project]
name = "example"
dependencies = [
    "requests>=2.28",
    "httpx>=0.27.0",
]
''')
    deps = _parse_pyproject_toml(str(f))
    assert deps['requests'] == '>=2.28'
    assert deps['httpx'] == '>=0.27.0'


def test_parse_package_json(tmp_path):
    f = tmp_path / 'package.json'
    f.write_text(json.dumps({
        'dependencies': {'express': '^4.18.0'},
        'devDependencies': {'jest': '^29.0.0'},
    }))
    deps = _parse_package_json(str(f))
    assert deps['express'] == '^4.18.0'
    assert deps['jest'] == '^29.0.0'


def test_parse_package_json_empty(tmp_path):
    f = tmp_path / 'package.json'
    f.write_text('{}')
    deps = _parse_package_json(str(f))
    assert deps == {}


# ── check_stale_projects tests ────────────────────────────────────────────

def test_stale_project_detected(tmp_path):
    p = _make_project(tmp_path, 'old-proj')
    settings = FakeSettings(paa_stale_days=30)
    old_ts = str(int(time.time()) - 90 * 86400)  # 90 days ago
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=old_ts)
        items = check_stale_projects([p], settings)
    assert len(items) == 1
    assert items[0].type == 'xp-stale-project'
    assert '90' in items[0].evidence or '89' in items[0].evidence


def test_recent_project_not_flagged(tmp_path):
    p = _make_project(tmp_path, 'fresh')
    settings = FakeSettings(paa_stale_days=60)
    recent_ts = str(int(time.time()) - 5 * 86400)  # 5 days ago
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=recent_ts)
        items = check_stale_projects([p], settings)
    assert len(items) == 0


def test_no_git_skipped(tmp_path):
    p = _make_project(tmp_path, 'no-git', with_git=False)
    items = check_stale_projects([p], FakeSettings())
    assert len(items) == 0


def test_empty_git_repo_skipped(tmp_path):
    p = _make_project(tmp_path, 'empty')
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout='')
        items = check_stale_projects([p], FakeSettings())
    assert len(items) == 0


def test_custom_threshold(tmp_path):
    p = _make_project(tmp_path, 'proj')
    settings = FakeSettings(paa_stale_days=7)
    ts_10_days = str(int(time.time()) - 10 * 86400)
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=ts_10_days)
        items = check_stale_projects([p], settings)
    assert len(items) == 1


# ── check_cross_references tests ─────────────────────────────────────────

def test_broken_relative_reference(tmp_path):
    p = _make_project(tmp_path, 'alpha', claude_md='See ../nonexistent/ for details')
    items = check_cross_references([p])
    assert len(items) == 1
    assert items[0].type == 'xp-broken-reference'
    assert 'nonexistent' in items[0].summary


def test_valid_relative_reference(tmp_path):
    sibling = _make_project(tmp_path, 'sibling')
    p = _make_project(tmp_path, 'alpha', claude_md='See ../sibling/ for details')
    items = check_cross_references([p, sibling])
    assert len(items) == 0


def test_no_claude_md_skipped(tmp_path):
    p = _make_project(tmp_path, 'bare')
    items = check_cross_references([p])
    assert len(items) == 0


# ── check_shared_dep_conflicts tests ──────────────────────────────────────

def test_conflicting_versions_detected(tmp_path):
    p1 = _make_project(tmp_path, 'a', manifest={'requirements.txt': 'httpx>=0.27.0\n'})
    p2 = _make_project(tmp_path, 'b', manifest={'requirements.txt': 'httpx>=0.20.0\n'})
    settings = FakeSettings(paa_allow_haiku=False)
    items, tokens = check_shared_dep_conflicts([p1, p2], settings)
    assert len(items) == 1
    assert items[0].type == 'xp-dep-conflict'
    assert 'httpx' in items[0].summary
    assert tokens == 0  # AI disabled


def test_same_versions_no_conflict(tmp_path):
    p1 = _make_project(tmp_path, 'a', manifest={'requirements.txt': 'httpx>=0.27.0\n'})
    p2 = _make_project(tmp_path, 'b', manifest={'requirements.txt': 'httpx>=0.27.0\n'})
    items, _ = check_shared_dep_conflicts([p1, p2], FakeSettings())
    assert len(items) == 0


def test_single_project_no_conflict(tmp_path):
    p = _make_project(tmp_path, 'solo', manifest={'requirements.txt': 'flask>=2.0\n'})
    items, _ = check_shared_dep_conflicts([p], FakeSettings())
    assert len(items) == 0


def test_no_manifests_no_items(tmp_path):
    p = _make_project(tmp_path, 'bare')
    items, _ = check_shared_dep_conflicts([p], FakeSettings())
    assert len(items) == 0


# ── Orchestrator test ─────────────────────────────────────────────────────

def test_orchestrator_aggregates(tmp_path):
    old = _make_project(tmp_path, 'old-proj')
    p1 = _make_project(tmp_path, 'a', manifest={'requirements.txt': 'flask>=2.0\n'})
    p2 = _make_project(tmp_path, 'b', manifest={'requirements.txt': 'flask>=1.0\n'})
    settings = FakeSettings(paa_stale_days=30, paa_allow_haiku=False)
    old_ts = str(int(time.time()) - 90 * 86400)
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=old_ts)
        items, tokens = run_cross_project_checks([old, p1, p2], settings)
    types = {i.type for i in items}
    assert 'xp-stale-project' in types
    assert 'xp-dep-conflict' in types


def test_orchestrator_graceful_failure(tmp_path):
    p = _make_project(tmp_path, 'proj')
    settings = FakeSettings()
    with patch('paa_cross_project.check_stale_projects', side_effect=RuntimeError):
        items, _ = run_cross_project_checks([p], settings)
    # Should not crash — other checks still run
    assert isinstance(items, list)
