"""P3.5f Item 1 (C9, David's second reveal): bundled-icon PLACEMENT pin.

Bench-proven: GTK4's unthemed ``IconTheme.add_search_path`` lookup resolves
files at the search-path ROOT and does NOT descend the freedesktop
``scalable/<context>/`` tree — ``has_icon('pm-sparkle-symbolic')`` was False at
``icons/scalable/actions/`` and True at ``icons/pm-sparkle-symbolic.svg``. So
the sparkle SVG must live at the icons/ root. This is a pure filesystem pin (no
GTK, no display) so it runs in the display-stripped suite and guards against a
regression that moves the asset back under the nested hierarchy.
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(REPO, 'icons')


def test_sparkle_icon_is_at_search_path_root():
    """BINDING: the sparkle SVG sits at the search-path ROOT, where GTK4's
    unthemed add_search_path lookup actually finds it."""
    root = os.path.join(ICONS, 'pm-sparkle-symbolic.svg')
    assert os.path.isfile(root), (
        'pm-sparkle-symbolic.svg must live at icons/ root for GTK4 unthemed '
        'add_search_path lookup to resolve it (bench-proven).'
    )


def test_sparkle_icon_not_under_scalable_actions():
    """BINDING (the trap): the SVG must NOT be under scalable/actions/, the
    freedesktop themed path GTK4's unthemed lookup ignores. The bug was the
    asset living here while has_icon() returned False."""
    nested = os.path.join(ICONS, 'scalable', 'actions', 'pm-sparkle-symbolic.svg')
    assert not os.path.exists(nested), (
        'pm-sparkle-symbolic.svg must NOT live under icons/scalable/actions/ — '
        "GTK4's unthemed add_search_path lookup does not resolve that tree."
    )
