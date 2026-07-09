"""G2 (reveal-3 item 2, constitution C10) — the PAA find-indicator arms on
standing pending findings, not just on a fresh edge.

The throb means "findings await you" (a LEVEL fact) but shipped EDGE-triggered
(``count > prev_count``) with ``prev_count`` seeded from the persisted ledger —
so 18 pending findings that survive a restart, or that exist when PAA is
enabled mid-session, showed the count and sat inert. The unseen-pending rule
fixes it via a pure decision in ``session.py`` (precedent: ``should_quit_app``).

Binding tests:
  T-G2a unseen=True, prev=18, count=18, closed → throb (the the maintainer repro)
  T-G2b after open (unseen=False), count=18, prev=18 → no re-throb
  T-G2c unseen=False, 18→19 → throb AND unseen re-arms True
  T-G2d window OPEN, any count → no throb (over-arming guard; T-G2a's pair)
  T-G2e count=0 → no throb
  T-G2f decrease (auto-resolve) with unseen=False → no throb
Window wiring (unbound-method idiom, see test_lifecycle.py):
  - _on_paa_findings_changed arms the throb for standing pending on first launch
  - _on_show_paa_window flips unseen so a later same-count emission is inert
"""
import types

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from session import paa_throb_decision
from window import AppWindow


# ── pure decision (the contract) ──────────────────────────────────────────────

def test_g2a_unseen_standing_pending_throbs():
    """T-G2a (the the maintainer repro): fresh launch (unseen=True), the very first
    emission delivers the 18 standing pending findings with prev==count (the
    ledger-seeded prev), window closed → throb. The edge rule missed this."""
    throb, unseen_after = paa_throb_decision(18, 18, True, False)
    assert throb is True
    assert unseen_after is True   # not grown → unseen carries unchanged (still True)


def test_g2b_seen_same_count_does_not_rethrob():
    """T-G2b: once the user has opened the window (unseen=False), a later
    emission of the SAME count must not nag — seen findings don't re-throb."""
    throb, unseen_after = paa_throb_decision(18, 18, False, False)
    assert throb is False
    assert unseen_after is False


def test_g2c_growth_throbs_and_rearms_unseen():
    """T-G2c: findings grow 18→19 even after a prior look (unseen=False) → throb
    (genuine news) AND unseen re-arms True (the new finding is itself unseen)."""
    throb, unseen_after = paa_throb_decision(19, 18, False, False)
    assert throb is True
    assert unseen_after is True


def test_g2d_window_open_never_throbs():
    """T-G2d (T-G2a's bidirectional pair, over-arming guard): with the window
    OPEN, no count throbs — the user is already looking. Inverting this gate is
    the mandated revert proof."""
    assert paa_throb_decision(18, 0, True, True)[0] is False
    assert paa_throb_decision(99, 18, True, True)[0] is False
    assert paa_throb_decision(5, 5, False, True)[0] is False


def test_g2e_zero_count_never_throbs():
    """T-G2e: nothing pending → no throb regardless of unseen/prev (the
    sidebar's stop-on-zero path is pinned separately in test_sidebar_state)."""
    assert paa_throb_decision(0, 18, True, False)[0] is False
    assert paa_throb_decision(0, 0, True, False)[0] is False


def test_g2f_decrease_is_not_news():
    """T-G2f: an auto-resolve sweep shrinks the count (18→12) with the findings
    already seen (unseen=False) → no throb. Fewer findings is not news."""
    throb, unseen_after = paa_throb_decision(12, 18, False, False)
    assert throb is False
    assert unseen_after is False


# ── window wiring (unbound methods against duck-typed recorders) ───────────────

def _wiring_fake(prev_count, unseen, paa_win=None):
    """A SimpleNamespace self for AppWindow._on_paa_findings_changed: records the
    sidebar throb/count calls; _paa_win=None means the card window is closed."""
    calls = []
    sidebar = types.SimpleNamespace()
    sidebar.set_paa_pending_count = lambda c: calls.append(('count', c))
    sidebar.start_paa_throb = lambda: calls.append(('throb',))
    fake = types.SimpleNamespace(
        _sidebar=sidebar,
        _paa_prev_count=prev_count,
        _paa_unseen=unseen,
        _paa_win=paa_win,
    )
    return fake, calls


def test_wiring_first_launch_standing_pending_arms_throb():
    """Window wiring: on a fresh launch (_paa_unseen=True) seeded with 18 pending
    from the ledger (_paa_prev_count=18), the first findings-changed emission of
    18 with the card closed calls start_paa_throb — the inert-button defect is
    dead."""
    fake, calls = _wiring_fake(prev_count=18, unseen=True, paa_win=None)
    AppWindow._on_paa_findings_changed(fake, None, 18)
    assert ('throb',) in calls
    assert fake._paa_prev_count == 18


def test_wiring_open_window_marks_seen_then_same_count_inert():
    """Window wiring: _on_show_paa_window flips _paa_unseen False (the user saw
    the findings); a subsequent same-count emission then does NOT re-throb."""
    sidebar = types.SimpleNamespace()
    sidebar.stop_paa_throb = lambda: None
    # _paa_win=None + _paa_ledger=None → the method sets unseen False then returns
    # at the no-ledger guard (we are pinning the unseen flip, not window build).
    show_fake = types.SimpleNamespace(
        _sidebar=sidebar, _paa_win=None, _paa_ledger=None, _paa_unseen=True)
    AppWindow._on_show_paa_window(show_fake, sidebar)
    assert show_fake._paa_unseen is False

    fake, calls = _wiring_fake(prev_count=18, unseen=False, paa_win=None)
    AppWindow._on_paa_findings_changed(fake, None, 18)
    assert ('throb',) not in calls
