"""Tests for pure virtual project-groups data layer."""
import json
import os
from unittest import mock

import pytest

from project_groups import (
    MAX_GROUP_DEPTH,
    DEFAULT_GROUPS_PATH,
    GroupForest,
    GroupNode,
    LoadResult,
    empty_forest,
    parse_forest,
    forest_to_dict,
    load_forest,
    load_forest_or_empty,
    save_forest,
    add_group,
    rename_group,
    delete_group,
    set_group_parent,
    set_group_expanded,
    set_membership,
    clear_membership,
    on_project_renamed,
    on_project_removed,
    prune_unknown_projects,
    depth_of,
    child_groups,
    projects_in,
    ungrouped_refs,
    can_reparent,
    group_path_names,
    build_tree_order,
    _project_sort_key,
)


# ---------------------------------------------------------------------------
# Constants / empty
# ---------------------------------------------------------------------------


def test_max_group_depth_constant():
    assert MAX_GROUP_DEPTH == 5


def test_default_groups_path():
    assert DEFAULT_GROUPS_PATH == os.path.expanduser(
        '~/.ProjectMan/project_groups.json'
    )


def test_empty_forest():
    f = empty_forest()
    assert f.groups == {}
    assert f.membership == {}


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def test_load_missing_file(tmp_path):
    path = str(tmp_path / 'nope.json')
    result = load_forest(path)
    assert isinstance(result, LoadResult)
    assert result.status == 'missing'
    assert result.error is None
    assert result.forest.groups == {}
    assert result.forest.membership == {}


def test_load_invalid_json(tmp_path):
    path = tmp_path / 'groups.json'
    path.write_text('not json!')
    result = load_forest(str(path))
    assert result.status == 'invalid'
    assert result.forest.groups == {}
    assert result.error is not None
    assert path.read_text() == 'not json!'  # left untouched


def test_load_invalid_top_level_type(tmp_path):
    path = tmp_path / 'groups.json'
    path.write_text('[1, 2, 3]')
    result = load_forest(str(path))
    assert result.status == 'invalid'
    assert result.forest.groups == {}
    assert result.error is not None


def test_load_oserror(tmp_path):
    path = str(tmp_path / 'groups.json')
    # Open raises PermissionError (subclass of OSError).
    with mock.patch('builtins.open', side_effect=PermissionError('denied')):
        result = load_forest(path)
    assert result.status == 'error'
    assert result.forest.groups == {}
    assert result.forest.membership == {}
    assert result.error is not None
    assert 'denied' in result.error


def test_load_forest_or_empty(tmp_path):
    path = str(tmp_path / 'nope.json')
    f = load_forest_or_empty(path)
    assert isinstance(f, GroupForest)
    assert f.groups == {}


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / 'groups.json')
    f = empty_forest()
    a = add_group(f, 'Alpha')
    b = add_group(f, 'Beta', parent_id=a.id)
    assert set_membership(f, 'local:/tmp/p1', b.id)
    save_forest(f, path)
    result = load_forest(path)
    assert result.status == 'ok'
    assert result.error is None
    f2 = result.forest
    assert set(f2.groups) == {a.id, b.id}
    assert f2.groups[a.id].name == 'Alpha'
    assert f2.groups[b.id].parent_id == a.id
    assert f2.membership['local:/tmp/p1'] == b.id
    assert f2.groups[a.id].expanded is True


def test_save_atomic_no_temp_files(tmp_path):
    path = str(tmp_path / 'groups.json')
    f = empty_forest()
    add_group(f, 'Only')
    save_forest(f, path)
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == 'groups.json'


def test_save_creates_directory(tmp_path):
    path = str(tmp_path / 'sub' / 'dir' / 'groups.json')
    f = empty_forest()
    add_group(f, 'X')
    save_forest(f, path)
    assert os.path.exists(path)


def test_forest_to_dict_schema():
    f = empty_forest()
    g = add_group(f, 'G', group_id='gid1')
    set_membership(f, 'local:/a', g.id)
    d = forest_to_dict(f)
    assert d['version'] == 1
    assert d['groups'] == [{
        'id': 'gid1',
        'name': 'G',
        'parent_id': None,
        'expanded': True,
    }]
    assert d['membership'] == {'local:/a': 'gid1'}


# ---------------------------------------------------------------------------
# add_group / depth
# ---------------------------------------------------------------------------


def test_add_group_root():
    f = empty_forest()
    g = add_group(f, 'Root')
    assert g.id in f.groups
    assert g.parent_id is None
    assert depth_of(f, g.id) == 1


def test_add_group_generates_unique_ids():
    f = empty_forest()
    a = add_group(f, 'A')
    b = add_group(f, 'B')
    assert a.id != b.id


def test_add_group_explicit_id():
    f = empty_forest()
    g = add_group(f, 'Named', group_id='fixed-id')
    assert g.id == 'fixed-id'
    with pytest.raises(ValueError):
        add_group(f, 'Dup', group_id='fixed-id')


def test_add_group_missing_parent_raises():
    f = empty_forest()
    with pytest.raises(ValueError, match='parent'):
        add_group(f, 'Orphan', parent_id='nope')


def test_add_group_empty_name_raises():
    f = empty_forest()
    with pytest.raises(ValueError):
        add_group(f, '  ')


def test_add_group_depth_limit():
    f = empty_forest()
    parent = None
    ids = []
    for i in range(MAX_GROUP_DEPTH):
        g = add_group(f, f'L{i + 1}', parent_id=parent)
        ids.append(g.id)
        parent = g.id
        assert depth_of(f, g.id) == i + 1
    # Depth 5 ok; depth 6 fails.
    assert depth_of(f, ids[-1]) == MAX_GROUP_DEPTH
    with pytest.raises(ValueError, match='max depth'):
        add_group(f, 'TooDeep', parent_id=ids[-1])


# ---------------------------------------------------------------------------
# rename / expand
# ---------------------------------------------------------------------------


def test_rename_group():
    f = empty_forest()
    g = add_group(f, 'Old')
    assert rename_group(f, g.id, 'New') is True
    assert f.groups[g.id].name == 'New'
    assert rename_group(f, 'missing', 'X') is False
    assert rename_group(f, g.id, '  ') is False


def test_set_group_expanded():
    f = empty_forest()
    g = add_group(f, 'G')
    assert g.expanded is True
    assert set_group_expanded(f, g.id, False) is True
    assert f.groups[g.id].expanded is False
    assert set_group_expanded(f, 'missing', True) is False


# ---------------------------------------------------------------------------
# reparent / cycles
# ---------------------------------------------------------------------------


def test_set_group_parent_and_can_reparent():
    f = empty_forest()
    a = add_group(f, 'A')
    b = add_group(f, 'B')
    c = add_group(f, 'C', parent_id=a.id)
    assert can_reparent(f, c.id, b.id) is True
    assert set_group_parent(f, c.id, b.id) is True
    assert f.groups[c.id].parent_id == b.id
    assert depth_of(f, c.id) == 2


def test_cycle_prevention_on_set_group_parent():
    f = empty_forest()
    a = add_group(f, 'A')
    b = add_group(f, 'B', parent_id=a.id)
    c = add_group(f, 'C', parent_id=b.id)
    # Cannot make A a child of C (would cycle).
    assert can_reparent(f, a.id, c.id) is False
    assert set_group_parent(f, a.id, c.id) is False
    assert f.groups[a.id].parent_id is None
    # Cannot parent to self.
    assert can_reparent(f, a.id, a.id) is False
    assert set_group_parent(f, b.id, b.id) is False


def test_reparent_depth_limit():
    f = empty_forest()
    # Chain of 4 under root-to-be; moving a height-2 tree under depth-4 fails.
    chain = []
    parent = None
    for i in range(4):
        g = add_group(f, f'C{i}', parent_id=parent)
        chain.append(g.id)
        parent = g.id
    # Separate height-2 tree at root.
    t = add_group(f, 'T')
    t_child = add_group(f, 'Tchild', parent_id=t.id)
    # Under chain[-1] (depth 4), T would be depth 5 and Tchild depth 6 → fail.
    assert can_reparent(f, t.id, chain[-1]) is False
    assert set_group_parent(f, t.id, chain[-1]) is False
    # Under chain[-2] (depth 3): T→4, Tchild→5 → ok.
    assert can_reparent(f, t.id, chain[-2]) is True
    assert set_group_parent(f, t.id, chain[-2]) is True
    assert depth_of(f, t_child.id) == MAX_GROUP_DEPTH


# ---------------------------------------------------------------------------
# delete_group
# ---------------------------------------------------------------------------


def test_delete_group_reparents_children_clears_membership():
    f = empty_forest()
    root = add_group(f, 'Root')
    mid = add_group(f, 'Mid', parent_id=root.id)
    leaf = add_group(f, 'Leaf', parent_id=mid.id)
    set_membership(f, 'local:/p1', mid.id)
    set_membership(f, 'local:/p2', leaf.id)
    set_membership(f, 'local:/p3', root.id)

    assert delete_group(f, mid.id) is True
    assert mid.id not in f.groups
    # Leaf reparented to root (mid's parent).
    assert f.groups[leaf.id].parent_id == root.id
    # Membership for mid cleared; others kept.
    assert 'local:/p1' not in f.membership
    assert f.membership['local:/p2'] == leaf.id
    assert f.membership['local:/p3'] == root.id


def test_delete_group_missing():
    f = empty_forest()
    assert delete_group(f, 'nope') is False


def test_delete_root_reparents_to_none():
    f = empty_forest()
    root = add_group(f, 'Root')
    child = add_group(f, 'Child', parent_id=root.id)
    assert delete_group(f, root.id) is True
    assert f.groups[child.id].parent_id is None
    assert depth_of(f, child.id) == 1


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


def test_membership_set_clear():
    f = empty_forest()
    g = add_group(f, 'G')
    assert set_membership(f, 'local:/x', g.id) is True
    assert projects_in(f, g.id) == ['local:/x']
    assert clear_membership(f, 'local:/x') is True
    assert projects_in(f, g.id) == []
    assert clear_membership(f, 'local:/x') is False


def test_membership_unknown_group_fails():
    f = empty_forest()
    assert set_membership(f, 'local:/x', 'missing') is False
    assert f.membership == {}


def test_membership_empty_ref_fails():
    f = empty_forest()
    g = add_group(f, 'G')
    assert set_membership(f, '', g.id) is False


def test_ungrouped_refs():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/a', g.id)
    f.membership['local:/ghost'] = 'deleted-group-id'
    all_refs = ['local:/a', 'local:/b', 'local:/ghost']
    ug = ungrouped_refs(f, all_refs)
    assert ug == ['local:/b', 'local:/ghost']


# ---------------------------------------------------------------------------
# project lifecycle helpers
# ---------------------------------------------------------------------------


def test_on_project_renamed():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/old', g.id)
    on_project_renamed(f, 'local:/old', 'local:/new')
    assert 'local:/old' not in f.membership
    assert f.membership['local:/new'] == g.id
    # No-op if old missing.
    on_project_renamed(f, 'local:/missing', 'local:/x')
    assert 'local:/x' not in f.membership


def test_on_project_renamed_collision_keeps_existing():
    f = empty_forest()
    g1 = add_group(f, 'G1')
    g2 = add_group(f, 'G2')
    set_membership(f, 'local:/old', g1.id)
    set_membership(f, 'local:/new', g2.id)
    on_project_renamed(f, 'local:/old', 'local:/new')
    assert 'local:/old' not in f.membership
    assert f.membership['local:/new'] == g2.id


def test_on_project_renamed_empty_new_ref_noop():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/old', g.id)
    on_project_renamed(f, 'local:/old', '')
    assert f.membership['local:/old'] == g.id
    on_project_renamed(f, 'local:/old', None)
    assert f.membership['local:/old'] == g.id


def test_on_project_removed():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/p', g.id)
    on_project_removed(f, 'local:/p')
    assert 'local:/p' not in f.membership
    on_project_removed(f, 'local:/p')  # idempotent


def test_prune_unknown_projects():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/keep', g.id)
    set_membership(f, 'local:/gone', g.id)
    set_membership(f, 'ssh:h:also-gone', g.id)
    n = prune_unknown_projects(f, {'local:/keep', 'local:/other'})
    assert n == 2
    assert set(f.membership) == {'local:/keep'}


# ---------------------------------------------------------------------------
# parse tolerance
# ---------------------------------------------------------------------------


def test_parse_none_and_empty():
    assert parse_forest(None).groups == {}
    assert parse_forest({}).groups == {}
    assert parse_forest('bad').groups == {}
    assert parse_forest(42).groups == {}


def test_parse_drops_bad_groups_and_membership():
    data = {
        'version': 1,
        'groups': [
            {'id': 'ok', 'name': 'OK', 'parent_id': None, 'expanded': True},
            {'id': '', 'name': 'empty-id'},
            {'name': 'no-id'},
            'not-a-dict',
            {'id': 'badname', 'name': 123},
            {'id': 'orphan', 'name': 'Orphan', 'parent_id': 'missing-parent'},
        ],
        'membership': {
            'local:/a': 'ok',
            'local:/b': 'unknown',
            99: 'ok',
            'local:/c': 5,
        },
    }
    f = parse_forest(data)
    assert set(f.groups) == {'ok', 'orphan'}
    assert f.groups['orphan'].parent_id is None  # missing parent fixed
    assert f.membership == {'local:/a': 'ok'}


def test_parse_drops_empty_whitespace_names():
    data = {
        'groups': [
            {'id': 'keep', 'name': 'Keep'},
            {'id': 'ws', 'name': '   '},
            {'id': 'empty', 'name': ''},
        ],
        'membership': {
            'local:/a': 'keep',
            'local:/orphan': 'ws',  # group dropped → membership orphaned
            'local:/orphan2': 'empty',
        },
    }
    f = parse_forest(data)
    assert set(f.groups) == {'keep'}
    assert f.membership == {'local:/a': 'keep'}


def test_parse_duplicate_group_ids_first_wins():
    data = {
        'groups': [
            {'id': 'dup', 'name': 'First'},
            {'id': 'dup', 'name': 'Second'},
        ],
        'membership': {},
    }
    f = parse_forest(data)
    assert set(f.groups) == {'dup'}
    assert f.groups['dup'].name == 'First'


def test_parse_breaks_cycles():
    data = {
        'groups': [
            {'id': 'a', 'name': 'A', 'parent_id': 'c'},
            {'id': 'b', 'name': 'B', 'parent_id': 'a'},
            {'id': 'c', 'name': 'C', 'parent_id': 'b'},
        ],
        'membership': {},
    }
    f = parse_forest(data)
    assert set(f.groups) == {'a', 'b', 'c'}
    # No cycles remain: every node has finite positive depth.
    for gid in f.groups:
        d = depth_of(f, gid)
        assert d >= 1
        assert d <= MAX_GROUP_DEPTH


def test_parse_clamps_over_depth():
    # Build a chain longer than MAX in raw data.
    groups = []
    for i in range(MAX_GROUP_DEPTH + 3):
        groups.append({
            'id': f'n{i}',
            'name': f'N{i}',
            'parent_id': f'n{i - 1}' if i else None,
        })
    f = parse_forest({'groups': groups, 'membership': {}})
    assert len(f.groups) == MAX_GROUP_DEPTH + 3
    for gid in f.groups:
        d = depth_of(f, gid)
        assert 1 <= d <= MAX_GROUP_DEPTH


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_child_groups_sorted():
    f = empty_forest()
    add_group(f, 'zeta', group_id='z')
    add_group(f, 'Alpha', group_id='a')
    add_group(f, 'beta', group_id='b')
    names = [g.name for g in child_groups(f, None)]
    assert names == ['Alpha', 'beta', 'zeta']


def test_group_path_names():
    f = empty_forest()
    a = add_group(f, 'A')
    b = add_group(f, 'B', parent_id=a.id)
    c = add_group(f, 'C', parent_id=b.id)
    assert group_path_names(f, c.id) == ['A', 'B', 'C']
    assert group_path_names(f, a.id) == ['A']
    assert group_path_names(f, 'missing') == []


def test_depth_of_missing():
    f = empty_forest()
    assert depth_of(f, 'nope') == 0


def test_project_sort_key_tiebreak_on_full_ref():
    # Same basename "foo" → order by full ref string for deterministic ties.
    a = 'local:/a/foo'
    b = 'local:/b/foo'
    c = 'ssh:h:foo'
    assert _project_sort_key(a) == ('foo', a)
    assert _project_sort_key(b) == ('foo', b)
    assert _project_sort_key(c) == ('foo', c)
    assert sorted([c, b, a], key=_project_sort_key) == [a, b, c]
    assert sorted([b, a], key=_project_sort_key) == [a, b]


# ---------------------------------------------------------------------------
# build_tree_order
# ---------------------------------------------------------------------------


def test_build_tree_order_structure():
    f = empty_forest()
    # Groups: Infrastructure > Network, empty Apps, and ungrouped projects.
    infra = add_group(f, 'Infrastructure', group_id='infra')
    net = add_group(f, 'Network', parent_id=infra.id, group_id='net')
    apps = add_group(f, 'Apps', group_id='apps')  # empty

    set_membership(f, 'local:/home/u/netbox', net.id)
    set_membership(f, 'ssh:h1:dns', net.id)
    set_membership(f, 'local:/home/u/prometheus', infra.id)
    # all_project_refs order is intentional mess; tree sorts projects by basename.
    all_refs = [
        'local:/home/u/zebra',
        'ssh:h1:dns',
        'local:/home/u/prometheus',
        'local:/home/u/netbox',
        'local:/home/u/alpha',
    ]
    order = build_tree_order(f, all_refs)

    # Full DFS: root groups (Apps, Infrastructure), under infra child groups
    # then projects, under net its projects, then host-root ungrouped.
    assert order == [
        ('group', 'apps', 0),
        ('group', 'infra', 0),
        ('group', 'net', 1),
        ('project', 'ssh:h1:dns', 2),
        ('project', 'local:/home/u/netbox', 2),
        ('project', 'local:/home/u/prometheus', 1),
        ('project', 'local:/home/u/alpha', 0),
        ('project', 'local:/home/u/zebra', 0),
    ]
    # Under infra: group net appears before project prometheus.
    kinds_under_infra = [
        t for t, i, d in order
        if (t == 'group' and i == 'net')
        or (t == 'project' and i == 'local:/home/u/prometheus')
    ]
    assert kinds_under_infra == ['group', 'project']


def test_build_tree_order_groups_before_projects_nested():
    """At every level: child groups before projects (file-manager style).

    Structure:
      G1 (root)
        G2 (child of G1)
        project-in-G1
      ungrouped project
    """
    f = empty_forest()
    g1 = add_group(f, 'G1', group_id='g1')
    g2 = add_group(f, 'G2', parent_id=g1.id, group_id='g2')
    set_membership(f, 'local:/proj-in-g1', g1.id)
    all_refs = ['local:/ungrouped', 'local:/proj-in-g1']
    order = build_tree_order(f, all_refs)

    assert order == [
        ('group', 'g1', 0),
        ('group', 'g2', 1),
        ('project', 'local:/proj-in-g1', 1),
        ('project', 'local:/ungrouped', 0),
    ]
    # Explicit: under G1, kinds are group then project.
    g1_idx = [i for i, (t, id_, _) in enumerate(order) if t == 'group' and id_ == 'g1'][0]
    under = order[g1_idx + 1: g1_idx + 3]
    assert under[0][0] == 'group' and under[0][1] == 'g2'
    assert under[1][0] == 'project' and under[1][1] == 'local:/proj-in-g1'


def test_build_tree_order_dedupes_duplicate_refs():
    f = empty_forest()
    g = add_group(f, 'G', group_id='g')
    set_membership(f, 'local:/dup', g.id)
    order = build_tree_order(
        f,
        ['local:/dup', 'local:/other', 'local:/dup', 'local:/other'],
    )
    projects = [i for t, i, _d in order if t == 'project']
    assert projects == ['local:/dup', 'local:/other']
    assert projects.count('local:/dup') == 1
    assert projects.count('local:/other') == 1


def test_build_tree_order_only_known_refs():
    f = empty_forest()
    g = add_group(f, 'G')
    set_membership(f, 'local:/known', g.id)
    set_membership(f, 'local:/stale', g.id)
    order = build_tree_order(f, ['local:/known'])
    refs = [i for t, i, _d in order if t == 'project']
    assert refs == ['local:/known']


def test_build_tree_order_empty():
    f = empty_forest()
    assert build_tree_order(f, []) == []
    assert build_tree_order(f, ['local:/only']) == [
        ('project', 'local:/only', 0),
    ]
