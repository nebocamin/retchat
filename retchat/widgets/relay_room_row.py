"""Sidebar row widgets for Reticulum Relay Chat (RRC) Hubs and Channels."""

from typing import Any, Callable, Dict, Optional

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk, Pango


class RelayHubRow(Gtk.ListBoxRow):
    """Header row representing a Reticulum Relay Chat (RRC) Hub.

    The buttons and menu use window actions with the hub hash as target
    (win.relay-hub-*), not Python signal handlers: a handler on a child widget
    that references the row forms a cycle through GTK that Python's garbage
    collector can't break, so every rebuilt row would stay in memory.
    """

    def __init__(self, hub_hash: str, hub_name: str, is_connected: bool, status_text: str):
        super().__init__()

        self.hub_hash = hub_hash.lower()
        self.hub_name = hub_name
        self.is_connected = is_connected
        self.status_text = status_text
        target = GLib.Variant.new_string(self.hub_hash)

        self.add_css_class("conversation-row")
        self.add_css_class("hub-header-row")

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main_box.set_margin_start(10)
        main_box.set_margin_end(6)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)

        # Server Icon
        self.icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        self.icon.set_pixel_size(22)
        self.icon.add_css_class("accent")
        main_box.append(self.icon)

        # Text Box
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        self.title_label = Gtk.Label(label=hub_name, xalign=0.0)
        self.title_label.add_css_class("conv-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(self.title_label)

        self.sub_label = Gtk.Label(xalign=0.0)
        self.sub_label.add_css_class("conv-subtitle")
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._update_subtitle()
        text_box.append(self.sub_label)

        main_box.append(text_box)

        # Quick '+' button to join channel on this hub
        self.add_btn = Gtk.Button(icon_name="list-add-symbolic", action_name="win.relay-hub-join-channel",
                                  action_target=target)
        self.add_btn.add_css_class("flat")
        self.add_btn.add_css_class("circular")
        self.add_btn.set_valign(Gtk.Align.CENTER)
        self.add_btn.set_tooltip_text("Kanal zu diesem Hub hinzufügen")
        main_box.append(self.add_btn)

        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.menu_btn.add_css_class("flat")
        self.menu_btn.add_css_class("circular")
        self.menu_btn.set_valign(Gtk.Align.CENTER)
        self.menu_btn.set_tooltip_text("Hub-Optionen")
        self._build_menu()
        main_box.append(self.menu_btn)

        self.set_child(main_box)

    def _build_menu(self):
        def item(label: str, action: str) -> Gio.MenuItem:
            menu_item = Gio.MenuItem.new(label, None)
            menu_item.set_action_and_target_value(action, GLib.Variant.new_string(self.hub_hash))
            return menu_item

        section = Gio.Menu()
        section.append_item(item("Kanal beitreten…", "win.relay-hub-join-channel"))
        if self.is_connected:
            section.append_item(item("Trennen", "win.relay-hub-disconnect"))
        else:
            section.append_item(item("Verbinden", "win.relay-hub-connect"))
        section.append_item(item("Hub-Adresse kopieren", "win.relay-hub-copy-address"))
        danger = Gio.Menu()
        danger.append_item(item("Hub entfernen", "win.relay-hub-remove"))

        menu = Gio.Menu()
        menu.append_section(None, section)
        menu.append_section(None, danger)
        self.menu_btn.set_menu_model(menu)

    def update_state(self, hub_name: str, is_connected: bool, status_text: str):
        self.hub_name = hub_name
        self.is_connected = is_connected
        self.status_text = status_text

        self.title_label.set_text(hub_name)
        self._update_subtitle()
        self._build_menu()

    def _update_subtitle(self):
        # The connection status is shown here only (no extra badge), leaving room for the name.
        self.sub_label.set_text(f"{self.status_text} · {self.hub_hash[:8]}…")


class RelayChannelRow(Gtk.ListBoxRow):
    """Row representing a joined channel/room under a Hub."""
    def __init__(
        self,
        hub_hash: str,
        room_name: str,
        hub_name: str,
        is_unread: bool = False,
        on_part_channel: Optional[Callable[[str, str], None]] = None
    ):
        super().__init__()

        self.hub_hash = hub_hash.lower()
        self.room_name = room_name
        self.hub_name = hub_name
        self.is_unread = is_unread
        self.on_part_channel = on_part_channel

        self.add_css_class("conversation-row")
        self.add_css_class("channel-row")

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main_box.set_margin_start(24)  # Indented under Hub
        main_box.set_margin_end(10)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)

        # Channel Hash / Icon
        # App symbolic icon (speech bubble); Adwaita has no chat-bubble icon
        self.icon = Gtk.Image.new_from_icon_name("org.selfmade.Retchat-symbolic")
        self.icon.set_pixel_size(18)
        self.icon.add_css_class("dim-label")
        main_box.append(self.icon)

        # Channel Name
        self.title_label = Gtk.Label(label=room_name, xalign=0.0)
        self.title_label.add_css_class("conv-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_hexpand(True)
        main_box.append(self.title_label)

        # Unread badge
        self.unread_badge = Gtk.Label(label="neu")
        self.unread_badge.add_css_class("badge")
        self.unread_badge.add_css_class("unread-badge")
        self.unread_badge.set_valign(Gtk.Align.CENTER)
        self.unread_badge.set_visible(self.is_unread)
        main_box.append(self.unread_badge)

        self.set_child(main_box)

    def set_unread(self, unread: bool):
        self.is_unread = unread
        self.unread_badge.set_visible(unread)


class RelayAddChannelRow(Gtk.ListBoxRow):
    """Helper row when a Hub has no channels or to add another channel."""
    def __init__(self, hub_hash: str, hub_name: str, on_add_channel: Callable[[str, str], None]):
        super().__init__()

        self.hub_hash = hub_hash.lower()
        self.hub_name = hub_name
        self.on_add_channel = on_add_channel

        self.add_css_class("conversation-row")

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        main_box.set_margin_start(24)
        main_box.set_margin_end(10)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)

        icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        icon.set_pixel_size(16)
        icon.add_css_class("accent")
        main_box.append(icon)

        lbl = Gtk.Label(label="Kanal hinzufügen...", xalign=0.0)
        lbl.add_css_class("accent")
        lbl.set_hexpand(True)
        main_box.append(lbl)

        self.set_child(main_box)
