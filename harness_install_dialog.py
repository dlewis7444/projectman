"""Missing-harness install dialog (M-UX.10a).

Replaces the old persistent toast with a popup that offers two clipboard
targets: the raw install command for the remote host, and an AI prompt blurb
for a local assistant to perform the install.
"""
from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk

from harnesses import SpawnFailureRecovery


def _set_clipboard(text: str) -> bool:
    try:
        Gdk.Display.get_default().get_clipboard().set(text)
        return True
    except Exception:
        return False


def present_harness_install_dialog(
    parent,
    recovery: SpawnFailureRecovery,
    *,
    on_copied=None,
):
    """Show the install-recovery dialog and copy blurbs on demand."""
    dialog = Adw.Dialog()
    dialog.set_title(recovery.dialog_title)
    if hasattr(dialog, 'set_content_width'):
        dialog.set_content_width(560)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    toolbar.add_top_bar(header)

    body = Gtk.Label(
        label=recovery.dialog_body,
        wrap=True,
        xalign=0,
        justify=Gtk.Justification.LEFT,
    )
    body.set_margin_top(12)
    body.set_margin_bottom(8)
    body.set_margin_start(24)
    body.set_margin_end(24)

    options = Adw.PreferencesGroup(
        title='Next steps',
        description='Copy one of the options below, then dismiss this dialog.',
    )

    if recovery.is_remote:
        cmd_subtitle = f'Paste into a shell on {recovery.host_label}'
        ai_subtitle = 'Paste into your local AI assistant'
    else:
        cmd_subtitle = 'Paste into a terminal on this machine'
        ai_subtitle = 'Paste into your local AI assistant'

    cmd_row = Adw.ActionRow(
        title='Copy install command',
        subtitle=cmd_subtitle,
    )
    cmd_btn = Gtk.Button(icon_name='edit-copy-symbolic')
    cmd_btn.set_valign(Gtk.Align.CENTER)
    cmd_row.add_suffix(cmd_btn)
    options.add(cmd_row)

    ai_row = Adw.ActionRow(
        title='Copy AI prompt',
        subtitle=ai_subtitle,
    )
    ai_btn = Gtk.Button(icon_name='edit-copy-symbolic')
    ai_btn.set_valign(Gtk.Align.CENTER)
    ai_row.add_suffix(ai_btn)
    options.add(ai_row)

    prefs = Adw.PreferencesPage()
    prefs.add(options)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_vexpand(True)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.append(body)
    content.append(prefs)
    scrolled.set_child(content)
    toolbar.set_content(scrolled)

    btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_box.set_halign(Gtk.Align.END)
    btn_box.set_margin_top(8)
    btn_box.set_margin_bottom(12)
    btn_box.set_margin_end(12)
    close_btn = Gtk.Button(label='Close')
    close_btn.add_css_class('suggested-action')
    btn_box.append(close_btn)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.append(toolbar)
    outer.append(btn_box)
    dialog.set_child(outer)

    def _notify(kind: str):
        if on_copied is not None:
            on_copied(kind)

    def _copy_cmd(*_a):
        if _set_clipboard(recovery.install_command_blurb):
            _notify('command')

    def _copy_ai(*_a):
        if _set_clipboard(recovery.ai_prompt_blurb):
            _notify('ai')

    def _close(*_a):
        dialog.close()

    cmd_btn.connect('clicked', _copy_cmd)
    cmd_row.connect('activated', _copy_cmd)
    ai_btn.connect('clicked', _copy_ai)
    ai_row.connect('activated', _copy_ai)
    close_btn.connect('clicked', _close)

    dialog.present(parent)
    return dialog