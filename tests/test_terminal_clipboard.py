"""Copy-URL / smart-copy clipboard write.

Regression for the 2026-06-26 report: right-click → "Copy URL" copied nothing.

History: an earlier revision of this test drove a ``wl-copy``/``xclip``
dispatch chosen because a *headless test window* couldn't propagate
``Gdk.Clipboard.set()`` cross-process. That was a harness artifact — the test
window never got real keyboard focus (Wayland focus-stealing prevention) —
and the wl-copy path was strictly worse on a real focused desktop (it pops its
own window and doesn't reliably take the clipboard from an already-focused
app). Reverted to ``Gdk.Clipboard.set()``, which works while the terminal holds
focus (the normal right-click case). See terminal.py:_set_clipboard.

These tests construct a real Vte-backed TerminalView, so they require a
display and are skipped without one (display_gate). Cross-process propagation
is focus-dependent and verified manually; the test asserts the set stores the
text and doesn't raise, via same-process clipboard readback.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
gi.require_version('GLib', '2.0')
from gi.repository import Gtk, Gdk, Vte, GLib

from display_gate import requires_display

pytestmark = requires_display


def _make_tv():
    from settings import Settings
    from model import Project
    from terminal import TerminalView
    proj = Project(name='test', path='/tmp/test')
    return TerminalView(proj, Settings())


def _read_clipboard(cb, timeout_ms=400):
    """Same-process read of Gdk.Clipboard via the async API + a nested loop.

    Same-process readback reflects what this process set even without keyboard
    focus, so it is a fair guard that ``_set_clipboard`` stored the text (and
    didn't raise). Cross-process propagation is the focus-dependent part, left
    to manual verification.
    """
    got = [None]
    done = [False]

    def finish(_o, res, _ud):
        try:
            got[0] = cb.read_text_finish(res)
        except Exception as e:  # empty clipboard etc.
            got[0] = f'<err {e!r}>'
        done[0] = True

    cb.read_text_async(None, finish, None)
    end = GLib.get_monotonic_time() + timeout_ms * 1000
    ctx = GLib.MainContext.default()
    while not done[0] and GLib.get_monotonic_time() < end:
        ctx.iteration(False)
    return got[0]


@requires_display
def test_set_clipboard_stores_text():
    tv = _make_tv()
    cb = Gdk.Display.get_default().get_clipboard()
    sentinel = 'file:///home/user/foo/bar.txt'
    tv._set_clipboard(sentinel)  # must not raise
    assert _read_clipboard(cb) == sentinel


@requires_display
def test_set_clipboard_stores_http_url():
    tv = _make_tv()
    cb = Gdk.Display.get_default().get_clipboard()
    sentinel = 'https://example.com/a/b?c=1#d'
    tv._set_clipboard(sentinel)
    assert _read_clipboard(cb) == sentinel


@requires_display
def test_set_clipboard_survives_display_unavailable():
    # If Gdk.Display.get_default() ever returns None (no display at call time),
    # _set_clipboard must swallow it rather than kill the context menu.
    tv = _make_tv()
    import terminal as _t
    orig = _t.Gdk.Display.get_default
    try:
        _t.Gdk.Display.get_default = staticmethod(lambda: None)
        tv._set_clipboard('resilient text')  # must not raise
    finally:
        _t.Gdk.Display.get_default = orig