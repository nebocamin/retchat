"""Conversation list row widget for the sidebar."""

import datetime
import re
from typing import Any, Dict

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Pango, GLib


def avatar_text(name: str) -> str:
    """Text for Adw.Avatar initials without punctuation ("Carol (LoRa)" -> "CL", not "C(")."""
    cleaned = re.sub(r"[^\w\s]", " ", name).strip()
    return cleaned or name


class ConversationRow(Gtk.ListBoxRow):
    def __init__(self, conv_data: Dict[str, Any]):
        super().__init__()
        self.conv_data = conv_data
        self.dest_hash = conv_data["destination_hash"].lower()

        self.action_row = Adw.ActionRow()
        self.action_row.set_activatable(False)

        # Avatar
        self.avatar = Adw.Avatar(size=40, show_initials=True)
        self.action_row.add_prefix(self.avatar)

        # Suffix container (Time + Unread Badge)
        self.suffix_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.suffix_box.set_valign(Gtk.Align.CENTER)

        self.time_label = Gtk.Label()
        self.time_label.add_css_class("dim-label")
        self.time_label.add_css_class("caption")
        self.time_label.set_halign(Gtk.Align.END)
        self.suffix_box.append(self.time_label)

        self.badge = Gtk.Label()
        self.badge.add_css_class("badge")
        self.badge.add_css_class("unread-badge")
        self.badge.set_halign(Gtk.Align.END)
        self.badge.set_visible(False)
        self.suffix_box.append(self.badge)

        self.action_row.add_suffix(self.suffix_box)
        self.set_child(self.action_row)

        self.update_data(conv_data)

    def update_data(self, conv_data: Dict[str, Any]):
        self.conv_data = conv_data

        # Name / Title
        custom_name = conv_data.get("custom_name")
        display_name = conv_data.get("display_name")
        dest_hash = conv_data.get("destination_hash", "")

        title = custom_name or display_name or f"[{dest_hash[:8]}...{dest_hash[-4:]}]"
        self.action_row.set_title(GLib.markup_escape_text(title))
        self.avatar.set_text(avatar_text(title))

        # Subtitle (Last message preview)
        last_msg = conv_data.get("last_message_text", "")
        if not last_msg:
            last_msg = f"Ziel: {dest_hash[:12]}..."
        # Limit subtitle length
        if len(last_msg) > 42:
            last_msg = last_msg[:42] + "..."
        self.action_row.set_subtitle(last_msg)

        # Timestamp
        last_time = conv_data.get("last_message_time", 0.0)
        if last_time:
            self.time_label.set_text(self._format_time(last_time))
            self.time_label.set_visible(True)
        else:
            self.time_label.set_visible(False)

        # Unread badge
        unread = conv_data.get("unread_count", 0)
        if unread > 0:
            self.badge.set_text(str(unread))
            self.badge.set_visible(True)
        else:
            self.badge.set_visible(False)

    def _format_time(self, timestamp: float) -> str:
        if not timestamp:
            return ""
        try:
            dt = datetime.datetime.fromtimestamp(timestamp)
            now = datetime.datetime.now()
            if dt.date() == now.date():
                return dt.strftime("%H:%M")
            elif dt.date() == (now - datetime.timedelta(days=1)).date():
                return "Gestern"
            elif dt.year == now.year:
                return dt.strftime("%d. %b")
            else:
                return dt.strftime("%d.%m.%y")
        except Exception:
            return ""
