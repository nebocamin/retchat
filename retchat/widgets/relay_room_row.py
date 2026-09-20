"""Sidebar row widget representing a Reticulum Relay Chat (RRC) room."""

from typing import Any, Dict

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango


class RelayRoomRow(Gtk.ListBoxRow):
    def __init__(self, hub_hash: str, room_name: str, hub_name: str, is_connected: bool, status_text: str, is_unread: bool = False):
        super().__init__()

        self.hub_hash = hub_hash
        self.room_name = room_name
        self.hub_name = hub_name
        self.is_connected = is_connected
        self.status_text = status_text
        self.is_unread = is_unread

        self.add_css_class("conversation-row")

        # Main horizontal layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        main_box.set_margin_top(8)
        main_box.set_margin_bottom(8)

        # Icon
        self.icon = Gtk.Image.new_from_icon_name("network-workgroup-symbolic")
        self.icon.set_pixel_size(24)
        self.icon.add_css_class("accent")
        main_box.append(self.icon)

        # Text Box
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        # Room Name
        self.title_label = Gtk.Label(label=room_name, xalign=0.0)
        self.title_label.add_css_class("conv-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(self.title_label)

        # Hub & Status subtitle
        self.sub_label = Gtk.Label(xalign=0.0)
        self.sub_label.add_css_class("conv-subtitle")
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._update_subtitle()
        text_box.append(self.sub_label)

        main_box.append(text_box)

        # Badges / status suffixes
        self.meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.meta_box.set_valign(Gtk.Align.CENTER)

        # Unread badge
        self.unread_badge = Gtk.Label(label="neu")
        self.unread_badge.add_css_class("badge")
        self.unread_badge.add_css_class("unread-badge")
        self.unread_badge.set_visible(self.is_unread)
        self.meta_box.append(self.unread_badge)

        # Status badge
        self.status_badge = Gtk.Label()
        self.status_badge.add_css_class("badge")
        self._update_status_badge()
        self.meta_box.append(self.status_badge)

        main_box.append(self.meta_box)
        self.set_child(main_box)

    def update_state(self, hub_name: str, is_connected: bool, status_text: str, is_unread: bool):
        self.hub_name = hub_name
        self.is_connected = is_connected
        self.status_text = status_text
        self.is_unread = is_unread

        self._update_subtitle()
        self._update_status_badge()
        self.unread_badge.set_visible(self.is_unread)

    def _update_subtitle(self):
        self.sub_label.set_text(f"{self.hub_name} • {self.status_text}")

    def _update_status_badge(self):
        self.status_badge.remove_css_class("hops-badge")
        self.status_badge.remove_css_class("dim-label")
        self.status_badge.remove_css_class("unread-badge")

        if self.is_connected:
            self.status_badge.set_text("Online")
            self.status_badge.add_css_class("hops-badge")
        elif "connecting" in self.status_text.lower():
            self.status_badge.set_text("Verbinde...")
            self.status_badge.add_css_class("unread-badge")
        else:
            self.status_badge.set_text("Getrennt")
            self.status_badge.add_css_class("dim-label")
