import os
import signal

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib


class ShutdownWindow(Adw.Window):
    """
    Modal progress window shown while sending SIGTERM to running Claude sessions.
    After 5 s of waiting, a Force Shutdown (SIGKILL) button is enabled.
    Cancel keeps PM open; the already-sent SIGTERMs will eventually be handled.
    """

    def __init__(self, parent, running, on_complete):
        """
        running    : dict[path → TerminalView]
        on_complete: callable() triggered when all processes have exited
        """
        super().__init__()
        self._running = running
        self._on_complete = on_complete
        self._remaining = set(running.keys())
        self._handler_ids = {}      # path → (tv, handler_id)
        self._row_widgets = {}      # path → (indicator_stack, status_label)
        self._force_timeout_id = None

        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_deletable(False)   # no X button — use Cancel instead
        self.set_resizable(False)
        self.set_default_size(400, -1)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(18)
        outer.set_margin_end(18)

        n = len(running)
        heading = Gtk.Label(
            label=f'Shutting down {n} session{"s" if n != 1 else ""}\u2026'
        )
        heading.add_css_class('title-4')
        heading.set_halign(Gtk.Align.START)
        outer.append(heading)

        # Per-project rows
        listbox = Gtk.ListBox()
        listbox.add_css_class('boxed-list')
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        for path, tv in running.items():
            name = os.path.basename(path)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.set_margin_top(8)
            box.set_margin_bottom(8)

            # Indicator: spinner → ✓
            ind = Gtk.Stack()
            ind.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            ind.set_transition_duration(200)
            spinner = Gtk.Spinner()
            spinner.start()
            ind.add_named(spinner, 'spinning')
            ok_icon = Gtk.Image.new_from_icon_name('emblem-ok-symbolic')
            ind.add_named(ok_icon, 'done')
            ind.set_visible_child_name('spinning')
            box.append(ind)

            name_lbl = Gtk.Label(label=name)
            name_lbl.set_halign(Gtk.Align.START)
            name_lbl.set_hexpand(True)
            box.append(name_lbl)

            status_lbl = Gtk.Label(label='shutting down\u2026')
            status_lbl.add_css_class('dim-label')
            status_lbl.add_css_class('caption')
            box.append(status_lbl)

            row.set_child(box)
            listbox.append(row)
            self._row_widgets[path] = (ind, status_lbl)

        outer.append(listbox)

        # Button row
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(4)

        self._cancel_btn = Gtk.Button(label='Cancel')
        self._cancel_btn.connect('clicked', self._on_cancel)
        btn_box.append(self._cancel_btn)

        self._force_btn = Gtk.Button(label='Force Shutdown')
        self._force_btn.add_css_class('destructive-action')
        self._force_btn.set_sensitive(False)
        self._force_btn.set_tooltip_text('Send SIGKILL to all remaining sessions')
        self._force_btn.connect('clicked', self._on_force)
        btn_box.append(self._force_btn)

        outer.append(btn_box)

        toolbar_view.set_content(outer)
        self.set_content(toolbar_view)

        # Connect signals BEFORE sending SIGTERM so we don't miss fast exits
        for path, tv in running.items():
            hid = tv.connect('process-exited',
                             lambda t, s, p=path: self._on_process_done(p))
            self._handler_ids[path] = (tv, hid)
            tv.deactivate()   # SIGTERM

        # Unlock Force Shutdown after 5 s if anything is still running
        self._force_timeout_id = GLib.timeout_add(5000, self._enable_force)

        self.present()

    # --- signal handlers ---

    def _on_process_done(self, path):
        self._remaining.discard(path)
        if path in self._row_widgets:
            ind, status_lbl = self._row_widgets[path]
            ind.set_visible_child_name('done')
            status_lbl.set_label('done')
        if not self._remaining:
            self._finish()

    def _enable_force(self):
        if self._remaining:
            self._force_btn.set_sensitive(True)
        return False     # don't repeat

    def _on_cancel(self, btn):
        """Keep PM open. Processes will exit on their own from the sent SIGTERM."""
        self._cleanup()
        self.destroy()

    def _on_force(self, btn):
        """SIGKILL anything still running, then close PM."""
        for path in list(self._remaining):
            tv = self._running.get(path)
            if tv and tv._child_pid is not None:
                try:
                    os.kill(tv._child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._finish()

    def _finish(self):
        self._cleanup()
        self.destroy()
        self._on_complete()   # destroy the main PM window

    def _cleanup(self):
        for tv, hid in self._handler_ids.values():
            try:
                tv.disconnect(hid)
            except Exception:
                pass
        self._handler_ids.clear()
        if self._force_timeout_id:
            GLib.source_remove(self._force_timeout_id)
            self._force_timeout_id = None
