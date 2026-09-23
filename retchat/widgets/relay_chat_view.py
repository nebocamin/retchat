"""Relay Chat View for Reticulum Relay Chat (RRC) rooms and hubs."""

from html import escape
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, Gtk, Pango

from retchat.models import RelayMessageItem
from retchat.widgets.chat_history import CONTENT_MAX_WIDTH, ChatHistory
from retchat.widgets.relay_message_row import RelayMessageRow, get_nick_color


class RelayChatView(Adw.Bin):
    """Shows either a relay room (chat) or a hub overview.

    The room chat uses ChatHistory, so it keeps the newest message in view the
    same way as the direct chat view.
    """

    def __init__(
        self,
        on_send_message: Callable[[str, str, str], None],
        on_part_room: Callable[[str, str], None],
        on_back_clicked: Optional[Callable[[], None]] = None,
        on_reconnect_hub: Optional[Callable[[str], None]] = None,
        on_join_channel_requested: Optional[Callable[[str, str], None]] = None,
        on_channel_selected: Optional[Callable[[str, str], None]] = None,
    ):
        super().__init__(hexpand=True, vexpand=True)
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

        self.history = ChatHistory(RelayMessageItem, RelayMessageRow)

        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.add_named(self._build_room_view(), "room_chat")
        self.view_stack.add_named(self._build_hub_overview(), "hub_overview")

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header())
        toolbar_view.set_content(self.view_stack)
        self.set_child(toolbar_view)

    # --- UI construction ------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Zurück zu den Chats")
        self.back_btn.connect("clicked", lambda _b: self.on_back_clicked() if self.on_back_clicked else None)
        header.pack_start(self.back_btn)

        self.window_title = Adw.WindowTitle(title="Relay-Chat")
        header.set_title_widget(self.window_title)

        # Members popover (only in room chat)
        self.members_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.members_list_box.add_css_class("members-popover")
        self.members_btn = Gtk.MenuButton(
            icon_name="system-users-symbolic",
            tooltip_text="Teilnehmer im Raum",
            popover=Gtk.Popover(child=self.members_list_box),
        )

        menu = Gio.Menu()
        menu.append("Kanal beitreten…", "win.relay_add_channel")
        menu.append("Neuen Relay-Hub hinzufügen…", "win.relay_add_hub")
        menu.append("Kanal verlassen", "win.relay_part_room")
        menu.append("Verbindung neu herstellen", "win.relay_reconnect")
        menu.append("Hub-Adresse kopieren", "win.relay_copy_hash")
        menu.append("Hub entfernen", "win.relay_remove_hub")
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Optionen", menu_model=menu))
        header.pack_end(self.members_btn)
        return header

    def _build_room_view(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=6)
        row.add_css_class("composer")

        self.entry = Gtk.Entry(
            placeholder_text="Nachricht (oder /me, /who, /part)",
            hexpand=True,
            valign=Gtk.Align.CENTER,
        )
        self.entry.connect("activate", lambda _e: self._send())
        self.entry.connect("changed", lambda _e: self.send_btn.set_sensitive(bool(self.entry.get_text().strip())))
        row.append(self.entry)

        self.send_btn = Gtk.Button(
            icon_name="mail-send-symbolic",
            tooltip_text="Senden (Enter)",
            valign=Gtk.Align.CENTER,
            sensitive=False,
            focus_on_click=False,  # keep the on-screen keyboard open
        )
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.connect("clicked", lambda _b: self._send())
        row.append(self.send_btn)

        room_view = Adw.ToolbarView(bottom_bar_style=Adw.ToolbarStyle.RAISED)
        room_view.set_content(self.history)
        room_view.add_bottom_bar(Adw.Clamp(child=row, maximum_size=CONTENT_MAX_WIDTH,
                                           tightening_threshold=CONTENT_MAX_WIDTH - 120))
        return room_view

    def _build_hub_overview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("hub-overview")

        self.hub_icon = Gtk.Image(icon_name="network-server-symbolic", pixel_size=64)
        self.hub_icon.add_css_class("accent")
        box.append(self.hub_icon)

        self.hub_title_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.hub_title_label.add_css_class("title-1")
        box.append(self.hub_title_label)

        self.hub_status_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                          justify=Gtk.Justification.CENTER)
        self.hub_status_label.add_css_class("dim-label")
        box.append(self.hub_status_label)

        self.hub_motd_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0.0)
        self.hub_motd_box = Adw.Bin(child=self.hub_motd_label)
        self.hub_motd_box.add_css_class("card")
        self.hub_motd_box.add_css_class("motd-card")
        box.append(self.hub_motd_box)

        self.hub_join_ch_btn = Gtk.Button(label="Kanal beitreten", halign=Gtk.Align.CENTER)
        self.hub_join_ch_btn.add_css_class("suggested-action")
        self.hub_join_ch_btn.add_css_class("pill")
        self.hub_join_ch_btn.connect("clicked", self._on_hub_join_channel_clicked)
        box.append(self.hub_join_ch_btn)

        self.hub_channels_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.hub_channels_box)

        return Gtk.ScrolledWindow(
            child=Adw.Clamp(child=box, maximum_size=480),
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
        )

    # --- Public API -----------------------------------------------------------

    def set_back_button_mode(self, is_collapsed: bool):
        if is_collapsed:
            self.back_btn.set_icon_name("go-previous-symbolic")
            self.back_btn.set_tooltip_text("Zurück zu den Chats")
        else:
            self.back_btn.set_icon_name("sidebar-show-symbolic")
            self.back_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")

    def load_room(
        self,
        hub_hash: str,
        room_name: str,
        hub_name: str,
        status_text: str,
        messages: List[Dict[str, Any]],
        members: List[Dict[str, str]],
    ):
        self.current_hub_hash = hub_hash.lower()
        self.current_room = room_name
        self.current_hub_name = hub_name

        self.window_title.set_title(room_name)
        self.window_title.set_subtitle(f"{hub_name} · {status_text}")
        self.entry.set_placeholder_text(f"Nachricht an {room_name}")
        self.members_btn.set_visible(True)
        self.update_members(members)

        items = []
        self._displayed_keys.clear()
        for msg in messages:
            item = RelayMessageItem(msg)
            if item.dedup_key not in self._displayed_keys:
                self._displayed_keys.add(item.dedup_key)
                items.append(item)

        self.view_stack.set_visible_child_name("room_chat")
        self.history.replace(items)
        self.entry.grab_focus()

    def load_hub(
        self,
        hub_hash: str,
        hub_name: str,
        status_text: str,
        is_connected: bool,
        motd: Optional[str],
        channels: List[str],
    ):
        self.current_hub_hash = hub_hash.lower()
        self.current_room = None
        self.current_hub_name = hub_name

        self.window_title.set_title(hub_name)
        self.window_title.set_subtitle(f"{status_text} · {hub_hash[:8]}…")
        self.members_btn.set_visible(False)

        self.hub_title_label.set_text(hub_name)
        self.hub_status_label.set_text(f"Status: {status_text} · Ziel: {hub_hash[:8]}…{hub_hash[-4:]}")

        self.hub_motd_label.set_text(motd or "")
        self.hub_motd_box.set_visible(bool(motd))

        while child := self.hub_channels_box.get_first_child():
            self.hub_channels_box.remove(child)

        if channels:
            heading = Gtk.Label(label=f"Beigetretene Kanäle ({len(channels)})")
            heading.add_css_class("heading")
            self.hub_channels_box.append(heading)
            for ch in channels:
                btn = Gtk.Button(label=f"Chat in {ch} öffnen", halign=Gtk.Align.CENTER)
                btn.add_css_class("pill")
                btn.connect("clicked", lambda _b, c=ch: self._on_channel_btn_clicked(c))
                self.hub_channels_box.append(btn)
        else:
            empty = Gtk.Label(label="Noch kein Kanal beigetreten.")
            empty.add_css_class("dim-label")
            self.hub_channels_box.append(empty)

        self.view_stack.set_visible_child_name("hub_overview")

    def update_status(self, status_text: str):
        if self.current_room and self.current_hub_name:
            self.window_title.set_subtitle(f"{self.current_hub_name} · {status_text}")
        elif self.current_hub_hash and self.current_hub_name:
            h = self.current_hub_hash
            self.window_title.set_subtitle(f"{status_text} · {h[:8]}…")
            self.hub_status_label.set_text(f"Status: {status_text} · Ziel: {h[:8]}…{h[-4:]}")

    def update_members(self, members: List[Dict[str, str]]):
        while child := self.members_list_box.get_first_child():
            self.members_list_box.remove(child)

        heading = Gtk.Label(label=f"Teilnehmer im Raum ({len(members)})", xalign=0.0)
        heading.add_css_class("heading")
        self.members_list_box.append(heading)

        if not members:
            empty = Gtk.Label(label="Keine bekannten Mitglieder", xalign=0.0)
            empty.add_css_class("dim-label")
            self.members_list_box.append(empty)
            return

        for m in members:
            nick = m.get("nick", "Unbekannt")
            row = Gtk.Box(spacing=8)
            nick_lbl = Gtk.Label(xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            nick_lbl.set_markup(f"<span foreground='{get_nick_color(nick)}' weight='bold'>{escape(nick)}</span>")
            row.append(nick_lbl)
            if m.get("hash"):
                hash_lbl = Gtk.Label(label=f"{m['hash'][:6]}…")
                hash_lbl.add_css_class("dim-label")
                hash_lbl.add_css_class("monospace")
                row.append(hash_lbl)
            self.members_list_box.append(row)

    def add_message(self, msg_data: Dict[str, Any]):
        item = RelayMessageItem(msg_data)
        if item.dedup_key in self._displayed_keys:
            return
        self._displayed_keys.add(item.dedup_key)
        self.history.append(item, scroll=item.is_me)

    # --- Internals ------------------------------------------------------------

    def _send(self):
        text = self.entry.get_text().strip()
        if not text or not (self.current_hub_hash and self.current_room):
            return
        self.entry.set_text("")
        self.history.scroll_to_bottom()
        self.on_send_message(self.current_hub_hash, self.current_room, text)

    def _on_channel_btn_clicked(self, channel_name: str):
        if self.on_channel_selected and self.current_hub_hash:
            self.on_channel_selected(self.current_hub_hash, channel_name)

    def _on_hub_join_channel_clicked(self, _btn):
        if self.on_join_channel_requested and self.current_hub_hash:
            self.on_join_channel_requested(self.current_hub_hash, self.current_hub_name)
