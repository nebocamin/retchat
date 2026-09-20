"""Relay Chat View for Reticulum Relay Chat (RRC) rooms."""

import datetime
from html import escape
import time
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Pango, Gdk, Gio

NICK_COLORS = [
    "#3584e4",  # blue
    "#33d17a",  # green
    "#e5a50a",  # yellow/gold
    "#e66100",  # orange
    "#9141ac",  # purple
    "#1c71d8",  # dark blue
    "#26a269",  # dark green
    "#c061cb",  # magenta
    "#f66151",  # red
]


def get_nick_color(nick: str) -> str:
    if not nick:
        return NICK_COLORS[0]
    idx = sum(ord(c) for c in nick) % len(NICK_COLORS)
    return NICK_COLORS[idx]


class RelayChatView(Gtk.Box):
    def __init__(
        self,
        on_send_message: Callable[[str, str, str], None],
        on_part_room: Callable[[str, str], None],
        on_back_clicked: Optional[Callable[[], None]] = None,
        on_reconnect_hub: Optional[Callable[[str], None]] = None
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.on_send_message = on_send_message
        self.on_part_room = on_part_room
        self.on_back_clicked = on_back_clicked
        self.on_reconnect_hub = on_reconnect_hub

        self.current_hub_hash: Optional[str] = None
        self.current_room: Optional[str] = None
        self.current_hub_name: str = ""

        # 1. Header Bar (Adw.HeaderBar automatically provides back button in collapsed mode)
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")

        # Window Title
        self.window_title = Adw.WindowTitle(title="Relay-Chat", subtitle="")
        self.header_bar.set_title_widget(self.window_title)

        # Members Button with Popover
        self.members_btn = Gtk.MenuButton(icon_name="system-users-symbolic")
        self.members_btn.set_tooltip_text("Teilnehmer im Raum")
        self.members_popover = Gtk.Popover()
        self.members_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.members_list_box.set_margin_top(8)
        self.members_list_box.set_margin_bottom(8)
        self.members_list_box.set_margin_start(12)
        self.members_list_box.set_margin_end(12)
        self.members_popover.set_child(self.members_list_box)
        self.members_btn.set_popover(self.members_popover)
        self.header_bar.pack_end(self.members_btn)

        # Menu Button (Actions)
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.menu_btn.set_tooltip_text("Optionen")
        self._build_menu()
        self.header_bar.pack_end(self.menu_btn)

        self.append(self.header_bar)

        # 2. Scrolled Messages Container
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_vexpand(True)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.messages_box.set_margin_top(12)
        self.messages_box.set_margin_bottom(12)
        self.messages_box.set_margin_start(10)
        self.messages_box.set_margin_end(10)
        self.messages_box.set_vexpand(True)
        self.scrolled_window.set_child(self.messages_box)

        self.append(self.scrolled_window)

        # 3. Composer Box
        self.composer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.composer_box.add_css_class("composer-bar")
        self.composer_box.set_margin_top(8)
        self.composer_box.set_margin_bottom(12)
        self.composer_box.set_margin_start(10)
        self.composer_box.set_margin_end(10)

        self.entry = Gtk.Entry()
        self.entry.set_width_chars(1)
        self.entry.set_placeholder_text("Nachricht schreiben (oder /me, /who, /part)...")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send_clicked)
        self.composer_box.append(self.entry)

        # Send Button
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.set_tooltip_text("Nachricht senden (Enter)")
        self.send_btn.connect("clicked", self._on_send_clicked)
        self.composer_box.append(self.send_btn)

        self.append(self.composer_box)

    def _build_menu(self):
        menu_model = Gio.Menu()
        menu_model.append("Verbindung neu herstellen", "win.relay_reconnect")
        menu_model.append("Hub-Adresse kopieren", "win.relay_copy_hash")
        menu_model.append("Raum verlassen", "win.relay_part_room")
        self.menu_btn.set_menu_model(menu_model)

    def load_room(self, hub_hash: str, room_name: str, hub_name: str, status_text: str, messages: List[Dict[str, Any]], members: List[Dict[str, str]]):
        self.current_hub_hash = hub_hash
        self.current_room = room_name
        self.current_hub_name = hub_name

        self.window_title.set_title(room_name)
        self.window_title.set_subtitle(f"{hub_name} • {status_text}")
        self.entry.set_placeholder_text(f"Nachricht an {room_name} verfassen...")

        # Update member popover
        self.update_members(members)

        # Clear existing messages
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)

        for msg in messages:
            self.add_message(msg, scroll_to_bottom=False)

        self._scroll_to_bottom()

    def update_status(self, status_text: str):
        if self.current_room and self.current_hub_name:
            self.window_title.set_subtitle(f"{self.current_hub_name} • {status_text}")

    def update_members(self, members: List[Dict[str, str]]):
        while child := self.members_list_box.get_first_child():
            self.members_list_box.remove(child)

        count = len(members)
        header_lbl = Gtk.Label(xalign=0.0)
        header_lbl.set_markup(f"<b>Teilnehmer im Raum ({count})</b>")
        header_lbl.set_margin_bottom(6)
        self.members_list_box.append(header_lbl)

        if not members:
            empty_lbl = Gtk.Label(label="Keine bekannten Mitglieder", xalign=0.0)
            empty_lbl.add_css_class("dim-label")
            self.members_list_box.append(empty_lbl)
            return

        for m in members:
            nick = m.get("nick", "Unbekannt")
            m_hex = m.get("hash", "")
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)

            color = get_nick_color(nick)
            nick_lbl = Gtk.Label(xalign=0.0)
            nick_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            nick_lbl.set_markup(f"<span foreground='{color}' weight='bold'>{escape(nick)}</span>")
            nick_lbl.set_hexpand(True)
            row_box.append(nick_lbl)

            if m_hex:
                hash_lbl = Gtk.Label(label=f"[{m_hex[:6]}...]", xalign=1.0)
                hash_lbl.add_css_class("dim-label")
                row_box.append(hash_lbl)

            self.members_list_box.append(row_box)

    def add_message(self, msg_data: Dict[str, Any], scroll_to_bottom: bool = True):
        kind = msg_data.get("kind", "msg")
        text = msg_data.get("text", "")
        nick = msg_data.get("nick") or "System"
        ts = msg_data.get("timestamp", time.time())
        is_me = bool(msg_data.get("is_me", False))

        time_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M")

        # System notices or channel events
        if kind in ("notice", "joined", "parted") or nick == "System":
            notice_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            notice_box.set_halign(Gtk.Align.CENTER)
            notice_box.set_margin_top(4)
            notice_box.set_margin_bottom(4)
            notice_box.set_margin_start(4)
            notice_box.set_margin_end(4)

            notice_label = Gtk.Label(
                xalign=0.5,
                justify=Gtk.Justification.CENTER,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR
            )
            notice_label.add_css_class("dim-label")
            notice_label.set_markup(f"<small>— {escape(text)} ({time_str}) —</small>")
            notice_box.append(notice_label)
            self.messages_box.append(notice_box)

        elif kind == "action":
            # /me action
            action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            action_box.set_halign(Gtk.Align.START if not is_me else Gtk.Align.END)
            action_box.set_margin_top(3)
            action_box.set_margin_bottom(3)
            action_box.set_margin_start(4)
            action_box.set_margin_end(4)

            color = get_nick_color(nick)
            action_label = Gtk.Label(
                xalign=0.0,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR
            )
            action_label.set_markup(f"<i>* <span foreground='{color}' weight='bold'>{escape(nick)}</span> {escape(text)}</i> <span alpha='65%' size='small'>{time_str}</span>")
            action_box.append(action_label)
            self.messages_box.append(action_box)

        else:
            # Regular chat bubble
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            row_box.set_margin_top(2)
            row_box.set_margin_bottom(2)

            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            bubble.set_size_request(80, -1)

            if is_me:
                row_box.set_halign(Gtk.Align.END)
                bubble.add_css_class("bubble-outgoing")
            else:
                row_box.set_halign(Gtk.Align.START)
                bubble.add_css_class("bubble-incoming")

                # Sender nick header for incoming messages in group chat
                color = get_nick_color(nick)
                nick_label = Gtk.Label(xalign=0.0)
                nick_label.set_ellipsize(Pango.EllipsizeMode.END)
                nick_label.set_markup(f"<span foreground='{color}' weight='bold'><small>{escape(nick)}</small></span>")
                bubble.append(nick_label)

            msg_label = Gtk.Label(
                label=text,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                selectable=True,
                xalign=0.0
            )
            msg_label.add_css_class("message-text")
            bubble.append(msg_label)

            # Footer with time
            time_label = Gtk.Label(label=time_str, xalign=1.0)
            time_label.add_css_class("message-time")
            time_label.set_halign(Gtk.Align.END)
            bubble.append(time_label)

            row_box.append(bubble)
            self.messages_box.append(row_box)

        if scroll_to_bottom:
            self._scroll_to_bottom()

    def _on_send_clicked(self, _widget):
        text = self.entry.get_text().strip()
        if not text:
            return

        if self.current_hub_hash and self.current_room:
            self.on_send_message(self.current_hub_hash, self.current_room, text)
            self.entry.set_text("")
            self.entry.grab_focus()

    def _scroll_to_bottom(self):
        def _scroll():
            adj = self.scrolled_window.get_vadjustment()
            if adj:
                adj.set_value(adj.get_upper() - adj.get_page_size())
            return False

        from gi.repository import GLib
        GLib.idle_add(_scroll)
