"""Relay Chat View for Reticulum Relay Chat (RRC) rooms and hubs."""

import datetime
from html import escape
import time
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Pango, Gio, GLib

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
        on_reconnect_hub: Optional[Callable[[str], None]] = None,
        on_join_channel_requested: Optional[Callable[[str, str], None]] = None,
        on_channel_selected: Optional[Callable[[str, str], None]] = None
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.on_send_message = on_send_message
        self.on_part_room = on_part_room
        self.on_back_clicked = on_back_clicked
        self.on_reconnect_hub = on_reconnect_hub
        self.on_join_channel_requested = on_join_channel_requested
        self.on_channel_selected = on_channel_selected

        self.current_hub_hash: Optional[str] = None
        self.current_room: Optional[str] = None
        self.current_hub_name: str = ""
        self._displayed_keys: set = set()

        # 1. Header Bar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")

        # Back / Sidebar Toggle button
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_btn.set_tooltip_text("Zurück zu den Chats")
        self.back_btn.add_css_class("flat")
        self.back_btn.connect("clicked", lambda _b: self.on_back_clicked() if self.on_back_clicked else None)
        self.header_bar.pack_start(self.back_btn)

        # Window Title
        self.window_title = Adw.WindowTitle(title="Relay-Chat", subtitle="")
        self.header_bar.set_title_widget(self.window_title)

        # Members Button with Popover (only in room chat)
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

        # View Stack: Room Chat vs Hub Overview
        self.view_stack = Gtk.Stack()
        self.view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.set_vexpand(True)

        # --- View 1: Room Chat ---
        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chat_box.set_vexpand(True)

        self.scrolled_window = Gtk.ScrolledWindow(vexpand=True)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.messages_box.set_margin_top(12)
        self.messages_box.set_margin_bottom(18)
        self.messages_box.set_margin_start(16)
        self.messages_box.set_margin_end(16)

        clamp = Adw.Clamp(maximum_size=700, child=self.messages_box)
        self.scrolled_window.set_child(clamp)
        self._is_at_bottom = True
        vadj = self.scrolled_window.get_vadjustment()
        if vadj:
            vadj.connect("value-changed", self._on_scroll_value_changed)
            vadj.connect("notify::upper", self._on_bounds_changed)
            vadj.connect("notify::page-size", self._on_bounds_changed)
        chat_box.append(self.scrolled_window)

        # Composer Box
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
        self.entry.connect("notify::has-focus", self._on_entry_focus)
        self.composer_box.append(self.entry)

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.set_focusable(False)
        self.send_btn.set_focus_on_click(False)
        self.send_btn.set_tooltip_text("Nachricht senden (Enter)")
        self.send_btn.connect("clicked", self._on_send_clicked)
        self.composer_box.append(self.send_btn)

        chat_box.append(self.composer_box)
        self.view_stack.add_named(chat_box, "room_chat")

        # --- View 2: Hub Overview ---
        self.hub_overview_scroll = Gtk.ScrolledWindow()
        self.hub_overview_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.hub_overview_scroll.set_vexpand(True)

        self.hub_overview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hub_overview_box.set_margin_top(24)
        self.hub_overview_box.set_margin_bottom(24)
        self.hub_overview_box.set_margin_start(16)
        self.hub_overview_box.set_margin_end(16)
        self.hub_overview_box.set_halign(Gtk.Align.CENTER)
        self.hub_overview_box.set_size_request(280, -1)

        # Hub Icon & Title
        self.hub_icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        self.hub_icon.set_pixel_size(56)
        self.hub_icon.add_css_class("accent")
        self.hub_overview_box.append(self.hub_icon)

        self.hub_title_label = Gtk.Label()
        self.hub_title_label.add_css_class("title-1")
        self.hub_overview_box.append(self.hub_title_label)

        self.hub_status_label = Gtk.Label()
        self.hub_status_label.add_css_class("dim-label")
        self.hub_status_label.set_wrap(True)
        self.hub_status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.hub_overview_box.append(self.hub_status_label)

        # MOTD Card
        self.hub_motd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.hub_motd_box.add_css_class("bubble-incoming")
        self.hub_motd_box.set_margin_top(8)
        self.hub_motd_box.set_margin_bottom(8)
        self.hub_motd_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0.0)
        self.hub_motd_box.append(self.hub_motd_label)
        self.hub_overview_box.append(self.hub_motd_box)

        # Action: Join Channel Button
        self.hub_join_ch_btn = Gtk.Button(label="Kanal beitreten")
        self.hub_join_ch_btn.add_css_class("suggested-action")
        self.hub_join_ch_btn.add_css_class("pill")
        self.hub_join_ch_btn.connect("clicked", self._on_hub_join_channel_clicked)
        self.hub_overview_box.append(self.hub_join_ch_btn)

        # Channels in this Hub
        self.hub_channels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hub_channels_box.set_margin_top(12)
        self.hub_overview_box.append(self.hub_channels_box)

        self.hub_overview_scroll.set_child(self.hub_overview_box)
        self.view_stack.add_named(self.hub_overview_scroll, "hub_overview")

        self.append(self.view_stack)

    def _build_menu(self):
        menu_model = Gio.Menu()
        menu_model.append("Kanal beitreten...", "win.relay_add_channel")
        menu_model.append("Neuen Relay-Hub hinzufügen...", "win.relay_add_hub")
        menu_model.append("Kanal verlassen", "win.relay_part_room")
        menu_model.append("Verbindung neu herstellen", "win.relay_reconnect")
        menu_model.append("Hub-Adresse kopieren", "win.relay_copy_hash")
        menu_model.append("Hub entfernen", "win.relay_remove_hub")
        self.menu_btn.set_menu_model(menu_model)

    def load_room(
        self,
        hub_hash: str,
        room_name: str,
        hub_name: str,
        status_text: str,
        messages: List[Dict[str, Any]],
        members: List[Dict[str, str]]
    ):
        self.current_hub_hash = hub_hash.lower()
        self.current_room = room_name
        self.current_hub_name = hub_name

        self.window_title.set_title(room_name)
        self.window_title.set_subtitle(f"{hub_name} • {status_text}")
        self.entry.set_placeholder_text(f"Nachricht an {room_name} verfassen...")
        self.members_btn.set_visible(True)

        self.update_members(members)

        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)
        self._displayed_keys.clear()

        for msg in messages:
            self.add_message(msg, scroll_to_bottom=False)

        self.view_stack.set_visible_child_name("room_chat")
        self._scroll_to_bottom()
        self.entry.grab_focus()

    def load_hub(
        self,
        hub_hash: str,
        hub_name: str,
        status_text: str,
        is_connected: bool,
        motd: Optional[str],
        channels: List[str]
    ):
        self.current_hub_hash = hub_hash.lower()
        self.current_room = None
        self.current_hub_name = hub_name

        self.window_title.set_title(hub_name)
        self.window_title.set_subtitle(f"{status_text} • {hub_hash[:8]}...")
        self.members_btn.set_visible(False)

        self.hub_title_label.set_text(hub_name)
        short_hash = f"{hub_hash[:8]}...{hub_hash[-4:]}"
        self.hub_status_label.set_text(f"Status: {status_text} • Ziel: {short_hash}")

        if motd:
            self.hub_motd_label.set_text(motd)
            self.hub_motd_box.set_visible(True)
        else:
            self.hub_motd_box.set_visible(False)

        # Clear and fill channels list
        while child := self.hub_channels_box.get_first_child():
            self.hub_channels_box.remove(child)

        if channels:
            lbl = Gtk.Label(xalign=0.5)
            lbl.set_markup(f"<b>Beigetretene Kanäle ({len(channels)})</b>")
            self.hub_channels_box.append(lbl)

            for ch in channels:
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row_box.set_halign(Gtk.Align.CENTER)
                btn = Gtk.Button(label=f"Chat in {ch} öffnen")
                btn.add_css_class("pill")
                btn.connect("clicked", lambda _b, c=ch: self._on_channel_btn_clicked(c))
                row_box.append(btn)
                self.hub_channels_box.append(row_box)
        else:
            lbl = Gtk.Label(label="Noch kein Kanal beigetreten.", xalign=0.5)
            lbl.add_css_class("dim-label")
            self.hub_channels_box.append(lbl)

        self.view_stack.set_visible_child_name("hub_overview")

    def _on_channel_btn_clicked(self, channel_name: str):
        if self.on_channel_selected and self.current_hub_hash:
            self.on_channel_selected(self.current_hub_hash, channel_name)

    def _on_hub_join_channel_clicked(self, _btn):
        if self.on_join_channel_requested and self.current_hub_hash:
            self.on_join_channel_requested(self.current_hub_hash, self.current_hub_name)

    def update_status(self, status_text: str):
        if self.current_room and self.current_hub_name:
            self.window_title.set_subtitle(f"{self.current_hub_name} • {status_text}")
        elif self.current_hub_hash and self.current_hub_name:
            self.window_title.set_subtitle(f"{status_text} • {self.current_hub_hash[:8]}...")
            short_hash = f"{self.current_hub_hash[:8]}...{self.current_hub_hash[-4:]}"
            self.hub_status_label.set_text(f"Status: {status_text} • Ziel: {short_hash}")

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
        src = msg_data.get("src", "")
        ts = msg_data.get("timestamp", time.time())

        # Deduplication check (ignoring sub-second differences)
        ts_sec = round(float(ts))
        msg_key = (kind, src, text, ts_sec)
        if msg_key in self._displayed_keys:
            return
        self._displayed_keys.add(msg_key)

        nick = msg_data.get("nick") or "System"
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

            time_label = Gtk.Label(label=time_str, xalign=1.0)
            time_label.add_css_class("message-time")
            time_label.set_halign(Gtk.Align.END)
            bubble.append(time_label)

            row_box.append(bubble)
            self.messages_box.append(row_box)

        if scroll_to_bottom:
            self._scroll_to_bottom()

    def set_back_button_mode(self, is_collapsed: bool):
        if is_collapsed:
            self.back_btn.set_icon_name("go-previous-symbolic")
            self.back_btn.set_tooltip_text("Zurück zu den Chats")
        else:
            self.back_btn.set_icon_name("sidebar-show-symbolic")
            self.back_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")

    def set_back_button_visible(self, visible: bool):
        self.back_btn.set_visible(visible)

    def _scroll_to_bottom(self):
        self._is_at_bottom = True
        adj = self.scrolled_window.get_vadjustment()
        if adj:
            adj.set_value(adj.get_upper())
        return False

    _scroll_after_layout = _scroll_to_bottom
    scroll_to_bottom = _scroll_to_bottom

    def _on_scroll_value_changed(self, adj):
        max_val = adj.get_upper() - adj.get_page_size()
        if max_val <= 0:
            self._is_at_bottom = True
        else:
            diff = max_val - adj.get_value()
            self._is_at_bottom = (diff <= 60.0)

    def _on_bounds_changed(self, adj, _pspec):
        if getattr(self, "_is_at_bottom", True):
            adj.set_value(adj.get_upper())

    def _on_entry_focus(self, entry, _pspec):
        if entry.has_focus():
            self._scroll_to_bottom()

    def _on_send_clicked(self, _widget):
        text = self.entry.get_text().strip()
        if not text:
            return

        if self.current_hub_hash and self.current_room:
            self.entry.set_text("")
            self._scroll_to_bottom()
            self.on_send_message(self.current_hub_hash, self.current_room, text)
