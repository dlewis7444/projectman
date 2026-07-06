"""The display gate for GTK-constructing tests — bench-only, by opt-in.

A developer's live desktop HAS a display, but running GTK tests there can
DBus-activate the REAL ProjectMan (shared application id): the 2026-06-10
fork incident — a builder's display-enabled suite runs activated the user's
live app four times, a duplicate-window bug spawned four extra windows, each
session-restored three projects, and one of the twelve stray `claude -c`
children pushed an unauthorized release. Display presence is therefore not
permission: the bench (the gated test bench) exports PM_TEST_DISPLAY_OK=1; nothing else
may.
"""
import os

import pytest

requires_display = pytest.mark.skipif(
    not (os.environ.get('PM_TEST_DISPLAY_OK') == '1'
         and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))),
    reason='display tests run only on the test bench '
           '(PM_TEST_DISPLAY_OK=1 plus a display)',
)
