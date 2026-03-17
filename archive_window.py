import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk


class ArchiveWindow(Adw.Window):
    def __init__(self, parent, store, on_restore):
        super().__init__()
        self._store = store
        self._on_restore = on_restore

        self.set_title('Archived Projects')
        self.set_default_size(480, 400)
        self.set_transient_for(parent)
        self.set_modal(False)
        self.connect('close-request', lambda w: w.destroy() or True)
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

        self._filter_text = ''

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text('Filter…')
        self._search_entry.connect('search-changed', self._on_search_changed)
        self._search_entry.connect('stop-search', self._on_search_stop)
        header.set_title_widget(self._search_entry)
        toolbar_view.add_top_bar(header)

        # Empty state
        self._empty = Adw.StatusPage()
        self._empty.set_title('No Archived Projects')
        self._empty.set_description(
            'Right-click a project and choose Archive to move it here.'
        )
        self._empty.set_icon_name('folder-symbolic')

        # List
        self._listbox = Gtk.ListBox()
        self._listbox.add_css_class('boxed-list')
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.set_filter_func(self._filter_row)
        self._listbox.set_margin_top(12)
        self._listbox.set_margin_bottom(12)
        self._listbox.set_margin_start(12)
        self._listbox.set_margin_end(12)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._listbox)
        box.append(self._empty)
        clamp.set_child(box)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

        self._populate()

    def _populate(self):
        while True:
            row = self._listbox.get_row_at_index(0)
            if row is None:
                break
            self._listbox.remove(row)

        projects = self._store.load_archived()
        n = len(projects)
        self.set_title(f'{n} Archived Projects' if n else 'Archived Projects')
        if not projects:
            self._listbox.set_visible(False)
            self._empty.set_visible(True)
            return

        self._listbox.set_visible(True)
        self._empty.set_visible(False)
        for proj in projects:
            self._listbox.append(self._make_row(proj))

    def _make_row(self, project):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        name = Gtk.Label(label=project.name)
        name.set_halign(Gtk.Align.START)
        name.set_hexpand(True)
        box.append(name)

        path = Gtk.Label(label=project.path)
        path.set_halign(Gtk.Align.END)
        path.add_css_class('dim-label')
        path.add_css_class('caption')
        box.append(path)

        btn = Gtk.Button(label='Restore')
        btn.add_css_class('suggested-action')
        btn.connect('clicked', lambda b, p=project: self._restore(p))
        box.append(btn)

        row.set_child(box)
        row._project = project
        return row

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_search_stop(self, entry):
        if entry.get_text():
            entry.set_text('')
        else:
            self.destroy()

    def _filter_row(self, row):
        if not self._filter_text:
            return True
        return hasattr(row, '_project') and self._filter_text in row._project.name.lower()

    def _on_search_changed(self, entry):
        self._filter_text = entry.get_text().lower()
        self._listbox.invalidate_filter()

    def _restore(self, project):
        self._on_restore(project)
        self._populate()
