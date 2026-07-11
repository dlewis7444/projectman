"""Opt-in gate for tests that construct real GTK widgets.

A developer's live desktop HAS a display, but running GTK tests there can
DBus-activate the REAL ProjectMan (shared application id): a display-enabled
suite once activated the user's live app multiple times, session-restored
projects into stray windows, and a child process took unintended actions.
Display presence is therefore not permission: tests that need GTK require
explicit ``PM_TEST_DISPLAY_OK=1`` plus ``DISPLAY`` / ``WAYLAND_DISPLAY``.
"""
import os

import pytest

requires_display = pytest.mark.skipif(
    not (os.environ.get('PM_TEST_DISPLAY_OK') == '1'
         and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))),
    reason='display tests require PM_TEST_DISPLAY_OK=1 plus a display',
)
