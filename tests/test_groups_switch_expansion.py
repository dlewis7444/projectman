# tests/test_groups_switch_expansion.py
"""Regression: switching projects (double-click activate / process-started)
must not collapse or hide the project groups in the sidebar.

the maintainer's report: "When switching to a new, active project in the same group or
in another group, the group in the sidebar collapses." — and, after the first
(overbroad) fix: "Now it doesn't switch the filter back to active-only."

Root cause (reproduced on the gated test bench under headless cage + pm-click, real
double-clicks): two mechanisms.

1. The M-UX.10b auto "Active Only" filter engages on every successful spawn
   (_on_started → set_active_only(True, path=...)). That flip is DESIRED —
   it returns the user to active-only after browsing Show-all. Group visibility
   under Active Only, revised by the maintainer 2026-07-26: a GroupRow survives only
   when it has an active (matching) descendant; groups with no active
   projects are hidden so the filtered view stays decluttered. The group
   holding the just-spawned project always survives, so the auto-flip still
   can't vanish the tree you're looking at. Expansion state is untouched.

2. A double-click on a project/session row inside a group's nested listbox
   fires row-activated on BOTH listboxes: inner activates the project
   (wanted), outer toggles the GroupRow (leaked collapse). Fix: a GroupRow
   activation immediately following a nested activation on the same ancestor
   chain is ignored (consumed per ancestor); header toggles and keyboard
   activation unaffected.
"""
import os
import sys

import pytest
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gtk, GLib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from display_gate import requires_display

pytestmark = requires_display

G1 = 'aaaa1111aaaa1111aaaa1111aaaa1111'
G2 = 'bbbb2222bbbb2222bbbb2222bbbb2222'


class _StubStore:
    def __init__(self, projects):
        self._projects = projects

    def load_projects(self):
        return list(self._projects)

    def load_archived(self):
        return []


class _StubWatcher:
    def get_project_status(self, project):
        return 'done'


def _drain():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _build_sidebar(mode='active', groups=True):
    from model import Project
    from project_groups import GroupForest, GroupNode
    from settings import Settings
    from sidebar import Sidebar

    projects = [
        Project(name='p1', path='/tmp/gc-p1'),
        Project(name='p2', path='/tmp/gc-p2'),
        Project(name='p3', path='/tmp/gc-p3'),
        Project(name='p4', path='/tmp/gc-p4'),
    ]
    forest = GroupForest(
        groups={
            G1: GroupNode(id=G1, name='G1', parent_id=None, expanded=True),
            G2: GroupNode(id=G2, name='G2', parent_id=None, expanded=True),
        },
        membership={
            'local:/tmp/gc-p1': G1,
            'local:/tmp/gc-p2': G1,
            'local:/tmp/gc-p3': G2,
            'local:/tmp/gc-p4': G2,
        },
    ) if groups else GroupForest()
    settings = Settings.load()
    settings.set_section_mode('localhost', mode)
    sb = Sidebar(_StubStore(projects), None, _StubWatcher(), settings=settings)
    sb.set_group_forest('localhost', forest)
    sb.refresh()
    _drain()
    return sb


def _grow(sb, gid):
    return sb._group_rows[('localhost', gid)]


def _revealed(grow):
    return grow._revealer.get_reveal_child()


def test_groups_stay_expanded_across_project_switch_active_mode():
    """Simulate the maintainer's switch in 'active' mode; both groups must stay open."""
    sb = _build_sidebar(mode='active')
    g1, g2 = _grow(sb, G1), _grow(sb, G2)
    assert _revealed(g1) and _revealed(g2)

    # p1 becomes active (spawned earlier) — G1 has an active descendant.
    sb.set_project_state('/tmp/gc-p1', 'attached', is_zellij=True)
    _drain()
    assert _revealed(g1), 'G1 collapsed when its project started'

    # the maintainer switches to p3 (in G2): the _switch_to_project + _on_started path.
    sb.select_project('/tmp/gc-p3')
    _drain()
    sb.set_project_state('/tmp/gc-p3', 'attached', is_zellij=True)
    sb.set_active_only(True, path='/tmp/gc-p3')
    _drain()

    assert _revealed(g1), 'G1 collapsed after switching to a project in G2'
    assert _revealed(g2), 'G2 collapsed after switching to its project'


def test_row_activated_on_nested_project_does_not_toggle_group():
    """Double-clicking a project inside a group must not collapse that group.

    GTK delivers the double-click to BOTH the nested child_listbox (activates
    the project — wanted) and the outer listbox (activates the GroupRow —
    leaked, unwanted toggle). The sidebar marks each nested activation's
    ancestor chain; a GroupRow activation arriving right after a nested one
    on that chain is a leak and must be ignored.
    """
    sb = _build_sidebar(mode='all')
    g2 = _grow(sb, G2)
    assert _revealed(g2)

    # The real double-click sequence: inner listbox fires for the project…
    row = sb._rows['/tmp/gc-p3']
    parent_lb = row.get_parent()
    parent_lb.emit('row-activated', row)
    _drain()
    # …then the outer listbox fires for the GroupRow (the leak).
    section_lb = g2.get_parent()
    section_lb.emit('row-activated', g2)
    _drain()
    assert _revealed(g2), 'leaked GroupRow activation collapsed the group'
    assert g2._group.expanded, 'leaked activation also touched durable state'

    # A genuine group activation (no nested activation just before) toggles.
    section_lb.emit('row-activated', g2)
    _drain()
    assert not _revealed(g2), 'genuine header activation must still toggle'
    # And back.
    section_lb.emit('row-activated', g2)
    _drain()
    assert _revealed(g2)


def test_switch_focus_survives_outer_listbox_steal():
    """End-to-end focus race: switching a project must leave keyboard focus in
    the terminal, not on the nav (the maintainer: "it focuses my keyboard on the nav
    menu"). A double-click inside a group's nested listbox also reaches the
    OUTER listbox's press gesture, which grabs focus to the GroupRow AFTER the
    activation handler ran (the leaked toggle is suppressed; the focus steal
    was not). The terminal grab in _switch_to_project must therefore be
    deferred to idle so it lands after the whole click sequence.

    Reproduced here deterministically at the widget level: run the real
    activation handler chain, then mimic the outer press gesture's
    grab_focus(GroupRow), then drain idle and check the window focus widget.
    A synchronous terminal grab would lose; the deferred one wins.
    """
    import types
    from window import AppWindow

    sb = _build_sidebar(mode='all')
    win = Gtk.Window()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    win.set_child(box)
    box.append(sb)
    terminal = Gtk.TextView()   # stand-in focusable "terminal"
    box.append(terminal)
    win.present()
    _drain()

    project = types.SimpleNamespace(path='/tmp/gc-p3', name='p3')
    tv = types.SimpleNamespace(_child_pid=1234, get_terminal=lambda: terminal)
    fake = types.SimpleNamespace(
        _sidebar=sb,
        _search_entry=types.SimpleNamespace(
            get_text=lambda: '', set_text=lambda t: None),
        _find_project=lambda path: project,
        _get_or_create_terminal=lambda p: tv,
        _stack=types.SimpleNamespace(set_visible_child_name=lambda n: None),
        _set_active_project=lambda p: None,
        _push_mru=lambda p: None,
        _active_path=None,
    )
    fake._switch_to_project = \
        lambda p: AppWindow._switch_to_project(fake, p)
    sb.connect('project-activated',
               lambda *a: AppWindow._on_project_activated(fake, *a))

    # The real double-click sequence on the nested p3 row: the inner listbox
    # activates the project (runs the full switch)…
    row = sb._rows['/tmp/gc-p3']
    row.get_parent().emit('row-activated', row)
    # …then the outer listbox's press gesture grabs focus to the GroupRow and
    # leaks an activation (suppressed toggle — tested above).
    g2 = _grow(sb, G2)
    g2.grab_focus()
    g2.get_parent().emit('row-activated', g2)
    _drain()

    try:
        assert win.get_focus() is terminal, (
            f'focus must land in the terminal after switching, '
            f'got {type(win.get_focus()).__name__}')
    finally:
        win.destroy()


def test_populate_rebuild_preserves_group_expansion():
    """A full rebuild (projects-changed watcher path) keeps groups open."""
    sb = _build_sidebar(mode='active')
    sb.set_project_state('/tmp/gc-p1', 'attached', is_zellij=True)
    _drain()
    sb.refresh()  # full _populate, as _on_projects_changed does
    _drain()
    g1, g2 = _grow(sb, G1), _grow(sb, G2)
    assert _revealed(g1), 'G1 collapsed across rebuild'
    assert _revealed(g2), 'G2 collapsed across rebuild'


def test_auto_active_only_flips_and_hides_groups_without_active():
    """Spawn auto Active-Only must engage; groups with no active project hide.

    _on_started (window.py) calls set_active_only(True, path=...) on every
    successful spawn — the maintainer's browse-Show-all → auto-return-to-active
    workflow depends on that flip. Per the maintainer 2026-07-26, Active Only shows a
    GroupRow only when it contains an active project; the group holding the
    just-spawned project always survives (no vanishing-tree regression), while
    empty-of-active groups drop out of the filtered view.
    """
    sb = _build_sidebar(mode='all')
    assert sb._section_mode('localhost') == 'all'

    # Simulate a successful spawn of p3 (in G2) — the _on_started sequence.
    sb.set_project_state('/tmp/gc-p3', 'attached', is_zellij=True)
    sb.set_active_only(True, path='/tmp/gc-p3')
    _drain()

    assert sb._section_mode('localhost') == 'active', \
        'auto Active-Only must engage on spawn (grouped hosts included)'
    g1, g2 = _grow(sb, G1), _grow(sb, G2)
    f = sb._filter_func_for('localhost')
    # G2 has the active p3 → survives; G1 has nothing active → hidden.
    assert f(g2), 'group holding the active project must survive the flip'
    assert not f(g1), 'group with no active project should hide in Active Only'
    # …while projects filter: active p3 passes, inactive p1 does not.
    assert f(sb._rows['/tmp/gc-p3'])
    assert not f(sb._rows['/tmp/gc-p1'])


def test_active_only_group_reappears_when_project_starts():
    """A hidden (empty-of-active) group returns when its project starts —
    the flip can never strand a running session inside an invisible group."""
    sb = _build_sidebar(mode='all')
    sb.set_project_state('/tmp/gc-p3', 'attached', is_zellij=True)
    sb.set_active_only(True, path='/tmp/gc-p3')
    _drain()
    f = sb._filter_func_for('localhost')
    assert not f(_grow(sb, G1))

    # p1 (in G1) starts → G1 now has an active descendant.
    sb.set_project_state('/tmp/gc-p1', 'attached', is_zellij=True)
    _drain()
    assert f(_grow(sb, G1)), 'G1 must reappear once its project is active'
    assert f(sb._rows['/tmp/gc-p1'])


def test_name_filter_still_hides_nonmatching_groups():
    """A name filter (not Active Only) still hides groups with no match."""
    sb = _build_sidebar(mode='all')
    sb._filter_text = 'p3'
    f = sb._filter_func_for('localhost')
    g1, g2 = _grow(sb, G1), _grow(sb, G2)
    assert not f(g1), 'name filter should hide non-matching G1'
    assert f(g2), 'name filter should keep G2 (p3 matches)'


def test_auto_active_only_still_fires_without_groups():
    """M-UX.10b preserved: group-less hosts keep the spawn auto-filter."""
    sb = _build_sidebar(mode='all', groups=False)
    sb.set_project_state('/tmp/gc-p3', 'attached', is_zellij=True)
    sb.set_active_only(True, path='/tmp/gc-p3')
    _drain()
    assert sb._section_mode('localhost') == 'active', \
        'auto Active-Only must still engage on group-less hosts'


def test_manual_active_toggle_still_works_with_groups():
    """The host-header manual toggle is not gated — only the auto path is."""
    sb = _build_sidebar(mode='all')
    sb.set_host_section_mode('localhost', 'active')
    _drain()
    assert sb._section_mode('localhost') == 'active'
