"""Row widget for discovered mesh peers (Announces)."""

import datetime
from typing import Any, Callable, Dict

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from retchat.widgets.conversation_row import avatar_text


class AnnounceRow(Gtk.ListBoxRow):
    def __init__(self, announce_data: Dict[str, Any], on_start_chat: Callable[[str, str], None]):
        super().__init__()
        self.announce_data = announce_data
        self.on_start_chat = on_start_chat
        self.dest_hash = announce_data["destination_hash"].lower()

        self.action_row = Adw.ActionRow()
        self.action_row.set_activatable(False)

        # Avatar
        self.avatar = Adw.Avatar(size=40, show_initials=True)
        self.action_row.add_prefix(self.avatar)

        # Suffix container (Hops badge + Chat button)
        self.suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.suffix_box.set_valign(Gtk.Align.CENTER)

        # Hops badge
        self.hops_label = Gtk.Label()
        self.hops_label.add_css_class("badge")
        self.hops_label.add_css_class("hops-badge")
        self.suffix_box.append(self.hops_label)

        # Start Chat button
        self.chat_btn = Gtk.Button(icon_name="mail-message-new-symbolic")
        self.chat_btn.add_css_class("flat")
        self.chat_btn.set_tooltip_text("Chat mit diesem Kontakt starten")
        self.chat_btn.connect("clicked", self._on_chat_clicked)
        self.suffix_box.append(self.chat_btn)

        self.action_row.add_suffix(self.suffix_box)
        self.set_child(self.action_row)

        self.update_data(announce_data)

    def update_data(self, announce_data: Dict[str, Any]):
        self.announce_data = announce_data
        dest_hash = announce_data.get("destination_hash", "")
        display_name = announce_data.get("display_name")
        hops = announce_data.get("hops", 0)
        iface = announce_data.get("receiving_interface", "")

        title = display_name or f"[{dest_hash[:8]}...{dest_hash[-4:]}]"
        self.action_row.set_title(GLib.markup_escape_text(title))
        self.avatar.set_text(avatar_text(title))

        sub_parts = [f"Ziel: {dest_hash[:12]}..."]
        if iface:
            # Extract clean interface name
            if "[" in iface and "]" in iface:
                iface_clean = iface.split("[")[-1].rstrip("]")
            else:
                iface_clean = iface
            sub_parts.append(iface_clean)

        self.action_row.set_subtitle(" • ".join(sub_parts))

        hops_text = "Lokal" if hops == 0 else f"{hops} Hop{'s' if hops > 1 else ''}"
        self.hops_label.set_text(hops_text)

    def _on_chat_clicked(self, _btn):
        name = self.announce_data.get("display_name") or ""
        self.on_start_chat(self.dest_hash, name)
