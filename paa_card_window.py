import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GLib


_TYPE_LABELS = {
    'missing-claude-md': 'Missing CLAUDE.md',
    'context-drift': 'Context Drift',
    'no-git': 'No Git Repo',
}

_SEVERITY_CSS = {
    'info': 'paa-card-type-info',
    'warning': 'paa-card-type-warning',
    'action-needed': 'paa-card-type-action',
}


class PAACardWindow(Adw.Window):
    """Card-based PAA window showing actionable findings."""

    def __init__(self, parent, ledger, settings, on_close=None):
        super().__init__()
        self._ledger = ledger
        self._settings = settings
        self._on_close_cb = on_close
        self._closing = False

        self.set_title('Projects Admin Agent')
        self.set_transient_for(parent)
        self.set_modal(False)

        pw = parent.get_width()
        ph = parent.get_height()
        self.set_default_size(min(700, int(pw * 0.8)), int(ph * 0.85))

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key)
        self.add_controller(key_ctrl)
        self.connect('close-request', self._on_close_request)
        self.connect('destroy', self._on_destroy)

        # Layout
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Stats row
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats.set_margin_start(16)
        stats.set_margin_end(16)
        stats.set_margin_top(12)
        stats.set_margin_bottom(8)
        self._pending_label = Gtk.Label()
        self._pending_label.add_css_class('paa-stats-count')
        stats.append(self._pending_label)
        content.append(stats)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(sep)

        # Scrollable card list
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_vexpand(True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._card_box.set_margin_start(8)
        self._card_box.set_margin_end(8)
        self._card_box.set_margin_top(8)
        self._card_box.set_margin_bottom(8)
        self._scrolled.set_child(self._card_box)
        content.append(self._scrolled)

        # Empty state
        self._empty = Adw.StatusPage()
        self._empty.set_vexpand(True)
        content.append(self._empty)

        toolbar.set_content(content)
        self.set_content(toolbar)
        self._refresh()

    def _refresh(self):
        # Clear cards
        while True:
            child = self._card_box.get_first_child()
            if child is None:
                break
            self._card_box.remove(child)

        items = self._ledger.pending_items()
        count = len(items)
        self._pending_label.set_label(
            f'{count} pending item{"s" if count != 1 else ""}'
        )
        has_items = count > 0
        self._scrolled.set_visible(has_items)
        self._empty.set_visible(not has_items)

        if not has_items:
            if not self._settings.paa_enabled:
                self._empty.set_title('PAA Disabled')
                self._empty.set_description(
                    'Enable the Projects Admin Agent in Settings \u2192 PAA'
                )
                self._empty.set_icon_name('system-shutdown-symbolic')
            else:
                self._empty.set_title('All Clear')
                self._empty.set_description(
                    'No issues found across your projects.'
                )
                self._empty.set_icon_name('emblem-ok-symbolic')
            return

        for item in items:
            self._card_box.append(self._build_card(item))

    def _build_card(self, item):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class('paa-card')

        # Header: type badge + project name
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        badge = Gtk.Label(label=_TYPE_LABELS.get(item.type, item.type))
        badge.add_css_class('paa-card-type')
        badge.add_css_class(_SEVERITY_CSS.get(item.severity, 'paa-card-type-info'))
        header.append(badge)
        proj_lbl = Gtk.Label(label=item.project)
        proj_lbl.add_css_class('paa-card-project')
        proj_lbl.set_hexpand(True)
        proj_lbl.set_halign(Gtk.Align.START)
        header.append(proj_lbl)
        card.append(header)

        # Summary
        summary = Gtk.Label(label=item.summary)
        summary.set_halign(Gtk.Align.START)
        summary.set_wrap(True)
        summary.set_xalign(0)
        summary.add_css_class('paa-card-summary')
        card.append(summary)

        # Evidence
        evidence = Gtk.Label(label=item.evidence)
        evidence.set_halign(Gtk.Align.START)
        evidence.set_wrap(True)
        evidence.set_xalign(0)
        evidence.add_css_class('paa-card-evidence')
        evidence.set_selectable(True)
        card.append(evidence)

        # Action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        actions.set_margin_top(4)

        dismiss_btn = Gtk.Button(label='Dismiss')
        dismiss_btn.add_css_class('flat')
        dismiss_btn.connect(
            'clicked', lambda b, iid=item.id: self._on_dismiss(iid)
        )
        actions.append(dismiss_btn)

        ack_btn = Gtk.Button(label='Acknowledge')
        ack_btn.add_css_class('suggested-action')
        ack_btn.connect(
            'clicked', lambda b, iid=item.id: self._on_acknowledge(iid)
        )
        actions.append(ack_btn)

        card.append(actions)
        return card

    def _on_dismiss(self, item_id):
        self._ledger.update_status(item_id, 'dismissed')
        self._ledger.save()
        self._refresh()

    def _on_acknowledge(self, item_id):
        self._ledger.update_status(item_id, 'approved')
        self._ledger.save()
        self._refresh()

    def refresh_from_scan(self):
        """Called when the monitor completes a scan."""
        self._refresh()

    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_close_request(self, window):
        if self._closing:
            return True
        self._closing = True
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
        GLib.idle_add(self.destroy)
        return True

    def _on_destroy(self, window):
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            cb()
