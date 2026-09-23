"""Main Application Window for Retchat."""

import time
from typing import Any, Dict, List, Optional, Tuple

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk

import RNS
from retchat.database import Database
from retchat.dialogs.add_relay_dialog import AddRelayDialog, COMMUNITY_HUB_HASH, COMMUNITY_HUB_NAME
from retchat.dialogs.join_channel_dialog import JoinChannelDialog
from retchat.dialogs.interfaces_dialog import InterfacesDialog
from retchat.dialogs.new_chat_dialog import NewChatDialog
from retchat.dialogs.profile_dialog import ProfileDialog
from retchat.reticulum_service import ReticulumService
from retchat.widgets.announce_row import AnnounceRow
from retchat.widgets.chat_view import ChatView
from retchat.widgets.conversation_row import ConversationRow
from retchat.widgets.relay_chat_view import RelayChatView
from retchat.widgets.relay_room_row import RelayHubRow, RelayChannelRow, RelayAddChannelRow


class RetchatWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, db: Database, service: ReticulumService):
        super().__init__(application=app)

        self.db = db
        self.service = service

        self.set_title("Retchat")
        self.set_default_size(860, 640)
        self.set_size_request(240, 180)

        self.current_dest_hash: Optional[str] = None
        self.current_relay_hub_hash: Optional[str] = None
        self.current_relay_room: Optional[str] = None
        self.conv_rows: Dict[str, ConversationRow] = {}
        self.announce_rows: Dict[str, AnnounceRow] = {}
        self.relay_hub_rows: Dict[str, RelayHubRow] = {}
        self.relay_channel_rows: Dict[Tuple[str, str], RelayChannelRow] = {}
        self.relay_add_channel_rows: Dict[str, RelayAddChannelRow] = {}

        # Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()

        # Split View (Sidebar + Content using Adw.OverlaySplitView)
        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_min_sidebar_width(260)
        self.split_view.set_max_sidebar_width(380)
        self.split_view.set_sidebar_width_fraction(0.32)
        self.split_view.set_enable_show_gesture(True)
        self.split_view.set_enable_hide_gesture(True)

        # Build Sidebar and Content
        sidebar_widget = self._build_sidebar()
        content_widget = self._build_content()

        self.split_view.set_sidebar(sidebar_widget)
        self.split_view.set_content(content_widget)

        # Mobile Breakpoint (e.g. for Phosh / PostmarketOS or narrow screens)
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 680px")
        )
        breakpoint.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # Narrow screen breakpoint (prevents min-sidebar-width from forcing window wide on mobile)
        narrow_bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 320px")
        )
        narrow_bp.add_setter(self.split_view, "min-sidebar-width", 220.0)
        self.add_breakpoint(narrow_bp)

        self.split_view.connect("notify::collapsed", self._on_split_collapsed_changed)

        # Initially show sidebar
        self.split_view.set_show_sidebar(True)

        self.toast_overlay.set_child(self.split_view)
        self.set_content(self.toast_overlay)

        self._update_back_buttons()

        # Setup GActions for window
        self._setup_actions()

        # Connect Service Callbacks
        self.service.add_message_received_callback(self._on_service_message_received)
        self.service.add_message_state_callback(self._on_service_message_state)
        self.service.add_announce_callback(self._on_service_announce_received)
        self.service.add_path_resolved_callback(self._on_service_path_resolved)
        self.service.add_conversations_changed_callback(self._on_service_conversations_changed)
        self.service.register_rrc_callbacks(
            on_message=self._on_rrc_message,
            on_change=self._on_rrc_change
        )

        # Initial data loading
        self._ensure_default_contact()
        self._load_conversations()
        self._load_announces()
        self._load_relay_rooms()

    def _setup_actions(self):
        # Action: Copy Hash (win.copy_hash)
        self.action_copy = Gio.SimpleAction.new("copy_hash", None)
        self.action_copy.connect("activate", lambda _a, _p: self._action_copy_hash())
        self.action_copy.set_enabled(False)
        self.add_action(self.action_copy)

        # Action: Rename Contact (win.rename)
        self.action_rename = Gio.SimpleAction.new("rename", None)
        self.action_rename.connect("activate", lambda _a, _p: self._action_rename_contact())
        self.action_rename.set_enabled(False)
        self.add_action(self.action_rename)

        # Action: Request Path (win.request_path)
        self.action_path = Gio.SimpleAction.new("request_path", None)
        self.action_path.connect("activate", lambda _a, _p: self._action_request_path())
        self.action_path.set_enabled(False)
        self.add_action(self.action_path)

        # Action: Sync (win.sync)
        self.action_sync = Gio.SimpleAction.new("sync", None)
        self.action_sync.connect("activate", lambda _a, _p: self._action_sync())
        self.action_sync.set_enabled(False)
        self.add_action(self.action_sync)

        # Action: Announce (win.announce)
        self.action_announce = Gio.SimpleAction.new("announce", None)
        self.action_announce.connect("activate", lambda _a, _p: self._action_announce_self())
        self.add_action(self.action_announce)

        # Action: Relay Reconnect (win.relay_reconnect)
        self.action_relay_reconnect = Gio.SimpleAction.new("relay_reconnect", None)
        self.action_relay_reconnect.connect("activate", lambda _a, _p: self._action_relay_reconnect())
        self.action_relay_reconnect.set_enabled(False)
        self.add_action(self.action_relay_reconnect)

        # Action: Relay Copy Hash (win.relay_copy_hash)
        self.action_relay_copy = Gio.SimpleAction.new("relay_copy_hash", None)
        self.action_relay_copy.connect("activate", lambda _a, _p: self._action_relay_copy_hash())
        self.action_relay_copy.set_enabled(False)
        self.add_action(self.action_relay_copy)

        # Action: Relay Part Room (win.relay_part_room)
        self.action_relay_part = Gio.SimpleAction.new("relay_part_room", None)
        self.action_relay_part.connect("activate", lambda _a, _p: self._action_relay_part_room())
        self.action_relay_part.set_enabled(False)
        self.add_action(self.action_relay_part)

        # Action: Relay Add Channel (win.relay_add_channel)
        self.action_relay_add_channel = Gio.SimpleAction.new("relay_add_channel", None)
        self.action_relay_add_channel.connect("activate", lambda _a, _p: self._action_relay_add_channel())
        self.action_relay_add_channel.set_enabled(False)
        self.add_action(self.action_relay_add_channel)

        # Action: Relay Remove Hub (win.relay_remove_hub)
        self.action_relay_remove_hub = Gio.SimpleAction.new("relay_remove_hub", None)
        self.action_relay_remove_hub.connect("activate", lambda _a, _p: self._action_relay_remove_hub())
        self.action_relay_remove_hub.set_enabled(False)
        self.add_action(self.action_relay_remove_hub)

        # Action: Relay Add Hub (win.relay_add_hub)
        self.action_relay_add_hub = Gio.SimpleAction.new("relay_add_hub", None)
        self.action_relay_add_hub.connect("activate", lambda _a, _p: self._show_add_relay_dialog())
        self.add_action(self.action_relay_add_hub)

    # --- UI Builders ---
    def _build_sidebar(self) -> Gtk.Widget:
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header Bar
        header = Adw.HeaderBar()
        header.add_css_class("flat")

        # Profile button
        self.profile_btn = Gtk.Button(icon_name="avatar-default-symbolic")
        self.profile_btn.set_tooltip_text("Eigene Identität & Profil")
        self.profile_btn.connect("clicked", lambda _b: self._show_profile_dialog())
        header.pack_start(self.profile_btn)

        # Title
        title_widget = Adw.WindowTitle(title="Retchat", subtitle="Reticulum Mesh")
        header.set_title_widget(title_widget)

        # Interfaces & Mesh status button
        iface_btn = Gtk.Button(icon_name="network-wireless-symbolic")
        iface_btn.set_tooltip_text("Schnittstellen, TCP-Hub & Propagation-Node")
        iface_btn.connect("clicked", lambda _b: self._show_interfaces_dialog())
        header.pack_end(iface_btn)

        # New chat button (+)
        self.add_btn = Gtk.Button(icon_name="list-add-symbolic")
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.set_tooltip_text("Neuen Chat starten")
        self.add_btn.connect("clicked", lambda _b: self._on_add_button_clicked())
        header.pack_end(self.add_btn)

        sidebar_box.append(header)

        # Search Entry
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Chats durchsuchen...")
        self.search_entry.set_margin_start(10)
        self.search_entry.set_margin_end(10)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(6)
        self.search_entry.connect("search-changed", self._on_search_changed)
        sidebar_box.append(self.search_entry)

        # View Switcher (Chats vs Entdecken vs Relay)
        stack_switcher = Gtk.StackSwitcher()
        stack_switcher.add_css_class("compact-switcher")
        stack_switcher.set_halign(Gtk.Align.CENTER)
        stack_switcher.set_margin_top(4)
        stack_switcher.set_margin_bottom(6)

        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.sidebar_stack.connect("notify::visible-child-name", self._on_sidebar_tab_changed)
        stack_switcher.set_stack(self.sidebar_stack)
        sidebar_box.append(stack_switcher)

        # Tab 1: Chats
        conv_scroll = Gtk.ScrolledWindow()
        conv_scroll.set_vexpand(True)
        conv_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.conv_list_box = Gtk.ListBox()
        self.conv_list_box.add_css_class("navigation-sidebar")
        self.conv_list_box.connect("row-selected", self._on_conv_selected)

        # Empty state for chats
        self.conv_empty_page = Adw.StatusPage()
        self.conv_empty_page.set_icon_name("mail-message-new-symbolic")
        self.conv_empty_page.set_title("Keine Chats")
        self.conv_empty_page.set_description("Starte einen Chat über das '+' Symbol oben.")
        self.conv_list_box.set_placeholder(self.conv_empty_page)

        conv_scroll.set_child(self.conv_list_box)
        self.sidebar_stack.add_titled(conv_scroll, "chats", "Chats")

        # Tab 2: Entdecken (Mesh Peers)
        announce_scroll = Gtk.ScrolledWindow()
        announce_scroll.set_vexpand(True)
        announce_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.announce_list_box = Gtk.ListBox()
        self.announce_list_box.add_css_class("navigation-sidebar")

        # Empty state for announces
        self.announce_empty_page = Adw.StatusPage()
        self.announce_empty_page.set_icon_name("network-transmit-receive-symbolic")
        self.announce_empty_page.set_title("Warte auf Mesh-Peers...")
        self.announce_empty_page.set_description(
            "Sobald Teilnehmer im Reticulum-Netzwerk ein Announce senden, erscheinen sie hier."
        )
        self.empty_announce_btn = Gtk.Button(label="Selbst im Mesh ankündigen")
        self.empty_announce_btn.add_css_class("suggested-action")
        self.empty_announce_btn.add_css_class("pill")
        self.empty_announce_btn.set_halign(Gtk.Align.CENTER)
        self.empty_announce_btn.connect("clicked", lambda _b: self._action_announce_self())
        self.announce_empty_page.set_child(self.empty_announce_btn)
        self.announce_list_box.set_placeholder(self.announce_empty_page)

        announce_scroll.set_child(self.announce_list_box)
        self.sidebar_stack.add_titled(announce_scroll, "discover", "Entdecken")

        # Tab 3: Relay (Reticulum Relay Chat Hubs & Rooms)
        relay_scroll = Gtk.ScrolledWindow()
        relay_scroll.set_vexpand(True)
        relay_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.relay_list_box = Gtk.ListBox()
        self.relay_list_box.add_css_class("navigation-sidebar")
        self.relay_list_box.connect("row-selected", self._on_relay_selected)

        # Empty state for relay
        self.relay_empty_page = Adw.StatusPage()
        self.relay_empty_page.set_icon_name("network-server-symbolic")
        self.relay_empty_page.set_title("Keine Relay-Hubs")
        self.relay_empty_page.set_description(
            "Tritt einem Relay-Chat-Hub (RRC) bei, um in Gruppenräumen über das Reticulum-Mesh zu chatten."
        )
        self.empty_relay_btn = Gtk.Button(label="Relay-Hub beitreten")
        self.empty_relay_btn.add_css_class("suggested-action")
        self.empty_relay_btn.add_css_class("pill")
        self.empty_relay_btn.set_halign(Gtk.Align.CENTER)
        self.empty_relay_btn.connect("clicked", lambda _b: self._show_add_relay_dialog())
        self.relay_empty_page.set_child(self.empty_relay_btn)
        self.relay_list_box.set_placeholder(self.relay_empty_page)

        relay_scroll.set_child(self.relay_list_box)
        self.sidebar_stack.add_titled(relay_scroll, "relay", "Relay")

        sidebar_box.append(self.sidebar_stack)
        return sidebar_box

    def _build_content(self) -> Gtk.Widget:
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_vexpand(True)
        self.content_stack.set_hexpand(True)

        # 1. Empty placeholder page
        empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        empty_header = Adw.HeaderBar()
        empty_header.add_css_class("flat")
        self.empty_sidebar_btn = Gtk.Button(icon_name="sidebar-show-symbolic")
        self.empty_sidebar_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")
        self.empty_sidebar_btn.add_css_class("flat")
        self.empty_sidebar_btn.connect("clicked", lambda _b: self._on_chat_back_clicked())
        empty_header.pack_start(self.empty_sidebar_btn)
        empty_box.append(empty_header)

        empty_page = Adw.StatusPage()
        empty_page.set_icon_name("network-wireless-symbolic")
        empty_page.set_title("Retchat")
        empty_page.set_description(
            "Wähle eine Unterhaltung aus oder starte einen neuen Chat über das Reticulum-Mesh-Netzwerk."
        )

        empty_btn = Gtk.Button(label="Neuen Chat starten")
        empty_btn.add_css_class("suggested-action")
        empty_btn.add_css_class("pill")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", lambda _b: self._show_new_chat_dialog())
        empty_page.set_child(empty_btn)
        empty_box.append(empty_page)

        self.content_stack.add_named(empty_box, "empty")

        # 2. Chat View
        self.chat_view = ChatView(
            on_back_clicked=self._on_chat_back_clicked,
            on_send_message=self._on_send_message,
            on_rename_contact=self._action_rename_contact,
            on_copy_hash=self._action_copy_hash,
            on_request_path=self._action_request_path
        )
        self.content_stack.add_named(self.chat_view, "chat")

        # 3. Relay Chat View
        self.relay_chat_view = RelayChatView(
            on_send_message=self._on_send_relay_message,
            on_part_room=self._on_part_relay_room,
            on_back_clicked=self._on_chat_back_clicked,
            on_reconnect_hub=self._on_reconnect_relay_hub,
            on_join_channel_requested=self._show_join_channel_dialog,
            on_channel_selected=self.open_relay_room
        )
        self.content_stack.add_named(self.relay_chat_view, "relay_chat")

        self.content_stack.set_visible_child_name("empty")
        return self.content_stack

    # --- Pre-populating Contact ---
    def _ensure_default_contact(self):
        """Pre-populate the user's specified contact if not present."""
        test_contact_hash = "8d883cfe6c1a846d8f34e5a95a149fdb"
        self.service.start_conversation(test_contact_hash)

    def _on_add_button_clicked(self):
        active_tab = self.sidebar_stack.get_visible_child_name() if hasattr(self, "sidebar_stack") else "chats"
        if active_tab == "relay":
            self._show_add_relay_dialog()
        else:
            self._show_new_chat_dialog()

    # --- Conversation List Management ---
    def _load_conversations(self, query: Optional[str] = None):
        conversations = self.service.get_conversations(query=query)

        # Clear existing rows
        while child := self.conv_list_box.get_first_child():
            self.conv_list_box.remove(child)
        self.conv_rows.clear()

        if query:
            self.conv_empty_page.set_title("Keine Chats gefunden")
            self.conv_empty_page.set_description(f"Keine Chats gefunden für «{query}».")
        else:
            self.conv_empty_page.set_title("Keine Chats")
            self.conv_empty_page.set_description("Starte einen Chat über das '+' Symbol oben.")

        for conv in conversations:
            row = ConversationRow(conv)
            self.conv_rows[conv["destination_hash"]] = row
            self.conv_list_box.append(row)

    def _on_search_changed(self, entry: Gtk.SearchEntry):
        q = entry.get_text().strip()
        active_tab = self.sidebar_stack.get_visible_child_name() if hasattr(self, "sidebar_stack") else "chats"
        if active_tab == "discover":
            self._load_announces(query=q if q else None)
        elif active_tab == "relay":
            self._load_relay_rooms(query=q if q else None)
        else:
            self._load_conversations(query=q if q else None)

    def _on_sidebar_tab_changed(self, stack: Gtk.Stack, _pspec):
        active_tab = stack.get_visible_child_name()
        q = self.search_entry.get_text().strip()
        if active_tab == "discover":
            self.search_entry.set_placeholder_text("Peers & Ankündigungen durchsuchen...")
            self.add_btn.set_tooltip_text("Neuen Chat starten")
            self._load_announces(query=q if q else None)
        elif active_tab == "relay":
            self.search_entry.set_placeholder_text("Hubs & Kanäle durchsuchen...")
            self.add_btn.set_tooltip_text("Relay-Hub beitreten")
            self._load_relay_rooms(query=q if q else None)
        else:
            self.search_entry.set_placeholder_text("Chats durchsuchen...")
            self.add_btn.set_tooltip_text("Neuen Chat starten")
            self._load_conversations(query=q if q else None)

    def _on_conv_selected(self, _list_box, row: Optional[ConversationRow]):
        if row is None:
            return
        dest_hash = row.dest_hash
        self.open_conversation(dest_hash)

    def open_conversation(self, dest_hash: str):
        dest_hash = dest_hash.lower()
        conv = self.service.get_conversation(dest_hash)
        if not conv:
            self.service.start_conversation(dest_hash)
            conv = self.service.get_conversation(dest_hash)

        self.current_dest_hash = dest_hash
        self.current_relay_hub_hash = None
        self.current_relay_room = None

        self.action_copy.set_enabled(True)
        self.action_rename.set_enabled(True)
        self.action_path.set_enabled(True)
        self.action_sync.set_enabled(True)

        self.action_relay_reconnect.set_enabled(False)
        self.action_relay_copy.set_enabled(False)
        self.action_relay_part.set_enabled(False)
        self.action_relay_add_channel.set_enabled(False)
        self.action_relay_remove_hub.set_enabled(False)
        self.action_relay_add_hub.set_enabled(False)

        if hasattr(self, "relay_list_box"):
            self.relay_list_box.unselect_all()

        self.service.mark_read(dest_hash)

        # Update row badge
        if dest_hash in self.conv_rows:
            conv["unread_count"] = 0
            self.conv_rows[dest_hash].update_data(conv)

        # Load messages
        messages = self.service.get_messages(dest_hash)
        self.chat_view.load_conversation(conv, messages)
        self.content_stack.set_visible_child_name("chat")

        # In collapsed (mobile) mode, hide sidebar overlay so chat takes over
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def _on_chat_back_clicked(self):
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(True)
        else:
            self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar())

    def _on_split_collapsed_changed(self, split_view, _pspec):
        is_collapsed = split_view.get_collapsed()
        if not is_collapsed:
            split_view.set_show_sidebar(True)
        else:
            if self.current_dest_hash or self.current_relay_room or self.current_relay_hub_hash:
                split_view.set_show_sidebar(False)
            else:
                split_view.set_show_sidebar(True)
        self._update_back_buttons()

    def _update_back_buttons(self):
        is_collapsed = self.split_view.get_collapsed()
        if hasattr(self, "chat_view"):
            self.chat_view.set_back_button_mode(is_collapsed)
        if hasattr(self, "relay_chat_view"):
            self.relay_chat_view.set_back_button_mode(is_collapsed)
        if hasattr(self, "empty_sidebar_btn"):
            self.empty_sidebar_btn.set_visible(not is_collapsed)

    # --- Relay Chat Management ---
    def _load_relay_rooms(self, query: Optional[str] = None):
        hubs = self.service.get_rrc_hubs()

        # Clear existing rows
        while child := self.relay_list_box.get_first_child():
            self.relay_list_box.remove(child)
        self.relay_hub_rows.clear()
        self.relay_channel_rows.clear()
        self.relay_add_channel_rows.clear()

        q = query.strip().lower() if query else None

        for hub_info in hubs:
            hub_hash = hub_info["hash"].lower()
            hub_name = hub_info["name"]
            is_connected = hub_info.get("is_connected", False)
            status_text = hub_info.get("status_text", "Getrennt")
            rooms = hub_info.get("rooms", [])
            unread_rooms = set(hub_info.get("unread_rooms", []))

            # Filter if search query is active
            if q:
                matching_rooms = [r for r in rooms if q in r.lower() or q in hub_name.lower() or q in hub_hash]
                if not (q in hub_name.lower() or q in hub_hash or matching_rooms):
                    continue
                display_rooms = matching_rooms if not (q in hub_name.lower() or q in hub_hash) else rooms
            else:
                display_rooms = rooms

            # 1. Hub Header Row
            hub_row = RelayHubRow(
                hub_hash=hub_hash,
                hub_name=hub_name,
                is_connected=is_connected,
                status_text=status_text,
                on_add_channel=self._show_join_channel_dialog,
                on_reconnect_hub=self._on_reconnect_relay_hub,
                on_disconnect_hub=self._on_disconnect_relay_hub,
                on_copy_hub_hash=self._copy_hub_hash_to_clipboard,
                on_remove_hub=self._confirm_remove_relay_hub
            )
            self.relay_hub_rows[hub_hash] = hub_row
            self.relay_list_box.append(hub_row)

            # 2. Channel Rows or Helper Row
            if display_rooms:
                for room in display_rooms:
                    is_unread = room in unread_rooms
                    ch_row = RelayChannelRow(
                        hub_hash=hub_hash,
                        room_name=room,
                        hub_name=hub_name,
                        is_unread=is_unread,
                        on_part_channel=self._on_part_relay_room
                    )
                    self.relay_channel_rows[(hub_hash, room)] = ch_row
                    self.relay_list_box.append(ch_row)
            elif not q:
                # No channels joined on this hub yet
                add_row = RelayAddChannelRow(
                    hub_hash=hub_hash,
                    hub_name=hub_name,
                    on_add_channel=self._show_join_channel_dialog
                )
                self.relay_add_channel_rows[hub_hash] = add_row
                self.relay_list_box.append(add_row)

        if q:
            self.relay_empty_page.set_title("Keine Hubs oder Räume gefunden")
            self.relay_empty_page.set_description(f"Keine Ergebnisse für «{query}».")
            if hasattr(self, "empty_relay_btn"):
                self.empty_relay_btn.set_visible(False)
        else:
            self.relay_empty_page.set_title("Keine Relay-Hubs")
            self.relay_empty_page.set_description(
                "Tritt einem Relay-Chat-Hub (RRC) bei, um in Gruppenräumen über das Reticulum-Mesh zu chatten."
            )
            if hasattr(self, "empty_relay_btn"):
                self.empty_relay_btn.set_visible(True)

    def _on_relay_selected(self, _list_box, row: Optional[Gtk.ListBoxRow]):
        if row is None:
            return
        if isinstance(row, RelayChannelRow):
            self.open_relay_room(row.hub_hash, row.room_name)
        elif isinstance(row, RelayHubRow):
            self.open_relay_hub(row.hub_hash)
        elif isinstance(row, RelayAddChannelRow):
            self._show_join_channel_dialog(row.hub_hash, row.hub_name)

    def open_relay_room(self, hub_hash: str, room_name: str):
        hub_hash = hub_hash.lower()
        self.current_relay_hub_hash = hub_hash
        self.current_relay_room = room_name
        self.current_dest_hash = None

        self.action_copy.set_enabled(False)
        self.action_rename.set_enabled(False)
        self.action_path.set_enabled(False)
        self.action_sync.set_enabled(False)

        self.action_relay_reconnect.set_enabled(True)
        self.action_relay_copy.set_enabled(True)
        self.action_relay_part.set_enabled(True)
        self.action_relay_add_channel.set_enabled(True)
        self.action_relay_remove_hub.set_enabled(True)
        self.action_relay_add_hub.set_enabled(True)

        self.service.mark_rrc_room_read(hub_hash, room_name)

        key = (hub_hash, room_name)
        if key in self.relay_channel_rows:
            self.relay_channel_rows[key].set_unread(False)

        if hasattr(self, "conv_list_box"):
            self.conv_list_box.unselect_all()

        hubs = self.service.get_rrc_hubs()
        hub_name = hub_hash[:8]
        status_text = "Getrennt"
        for h in hubs:
            if h["hash"].lower() == hub_hash:
                hub_name = h["name"]
                status_text = h["status_text"]
                break

        messages = self.service.get_rrc_messages(hub_hash, room_name)
        members = self.service.get_rrc_members(hub_hash, room_name)

        self.relay_chat_view.load_room(
            hub_hash=hub_hash,
            room_name=room_name,
            hub_name=hub_name,
            status_text=status_text,
            messages=messages,
            members=members
        )

        self.content_stack.set_visible_child_name("relay_chat")
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def open_relay_hub(self, hub_hash: str):
        hub_hash = hub_hash.lower()
        self.current_relay_hub_hash = hub_hash
        self.current_relay_room = None
        self.current_dest_hash = None

        self.action_copy.set_enabled(False)
        self.action_rename.set_enabled(False)
        self.action_path.set_enabled(False)
        self.action_sync.set_enabled(False)

        self.action_relay_reconnect.set_enabled(True)
        self.action_relay_copy.set_enabled(True)
        self.action_relay_part.set_enabled(False)
        self.action_relay_add_channel.set_enabled(True)
        self.action_relay_remove_hub.set_enabled(True)
        self.action_relay_add_hub.set_enabled(True)

        if hasattr(self, "conv_list_box"):
            self.conv_list_box.unselect_all()

        hubs = self.service.get_rrc_hubs()
        hub_name = hub_hash[:8]
        status_text = "Getrennt"
        is_connected = False
        motd = None
        rooms = []
        for h in hubs:
            if h["hash"].lower() == hub_hash:
                hub_name = h["name"]
                status_text = h["status_text"]
                is_connected = h.get("is_connected", False)
                motd = h.get("motd")
                rooms = h.get("rooms", [])
                break

        self.relay_chat_view.load_hub(
            hub_hash=hub_hash,
            hub_name=hub_name,
            status_text=status_text,
            is_connected=is_connected,
            motd=motd,
            channels=rooms
        )

        self.content_stack.set_visible_child_name("relay_chat")
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def _show_join_channel_dialog(self, hub_hash: str, hub_name: Optional[str] = None):
        if not hub_name:
            hubs = self.service.get_rrc_hubs()
            for h in hubs:
                if h["hash"].lower() == hub_hash.lower():
                    hub_name = h["name"]
                    break
            if not hub_name:
                hub_name = hub_hash[:8]

        dialog = JoinChannelDialog(
            parent_window=self,
            service=self.service,
            hub_hash=hub_hash,
            hub_name=hub_name,
            on_channel_joined=self._on_channel_joined
        )
        dialog.present()

    def _on_channel_joined(self, hub_hash: str, room_name: str):
        self._load_relay_rooms()
        self.open_relay_room(hub_hash, room_name)
        self._show_toast(f"Kanal {room_name} beigetreten")

    def _on_send_relay_message(self, hub_hash: str, room_name: str, text: str):
        try:
            trimmed = text.strip()
            if trimmed.startswith("/join "):
                new_channel = trimmed[6:].strip()
                if new_channel:
                    if not new_channel.startswith("#"):
                        new_channel = "#" + new_channel
                    self.service.join_rrc_room(hub_hash, new_channel)
                    self._load_relay_rooms()
                    self.open_relay_room(hub_hash, new_channel)
                    self._show_toast(f"Kanal {new_channel} beigetreten")
                    return
            elif trimmed == "/part":
                self._on_part_relay_room(hub_hash, room_name)
                return

            success, msg = self.service.send_rrc_message(hub_hash, room_name, text)
            if not success:
                self._show_toast(msg)
        except Exception as e:
            self._show_toast(f"Fehler: {e}")

    def _on_part_relay_room(self, hub_hash: str, room_name: str):
        self.service.part_rrc_room(hub_hash, room_name)
        self._show_toast(f"{room_name} verlassen")
        self._load_relay_rooms()

        if self.current_relay_hub_hash == hub_hash and self.current_relay_room == room_name:
            hubs = self.service.get_rrc_hubs()
            remaining_rooms = []
            for h in hubs:
                if h["hash"].lower() == hub_hash.lower():
                    remaining_rooms = h.get("rooms", [])
                    break
            if remaining_rooms:
                self.open_relay_room(hub_hash, remaining_rooms[0])
            else:
                self.open_relay_hub(hub_hash)

    def _on_reconnect_relay_hub(self, hub_hash: str):
        self.service.connect_rrc_hub(hub_hash)
        self._show_toast("Verbindung wird hergestellt...")

    def _on_disconnect_relay_hub(self, hub_hash: str):
        self.service.disconnect_rrc_hub(hub_hash)
        self._show_toast("Getrennt")
        self._load_relay_rooms()
        if self.current_relay_hub_hash == hub_hash.lower():
            if self.current_relay_room:
                self.relay_chat_view.update_status("Getrennt")
            else:
                self.open_relay_hub(hub_hash)

    def _copy_hub_hash_to_clipboard(self, hub_hash: str):
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(hub_hash)
        self._show_toast("Hub-Adresse kopiert!")

    def _confirm_remove_relay_hub(self, hub_hash: str):
        hub_hash = hub_hash.lower()
        hubs = self.service.get_rrc_hubs()
        hub_name = hub_hash[:8]
        for h in hubs:
            if h["hash"].lower() == hub_hash:
                hub_name = h["name"]
                break

        dialog = Adw.AlertDialog(
            heading=f"Hub «{hub_name}» entfernen?",
            body="Möchtest du diesen Relay-Hub und alle zugehörigen Kanäle wirklich entfernen?"
        )
        dialog.add_response("cancel", "Abbrechen")
        dialog.add_response("remove", "Entfernen")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response):
            if response == "remove":
                self._action_remove_relay_hub_direct(hub_hash, hub_name)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _action_remove_relay_hub_direct(self, hub_hash: str, hub_name: str = ""):
        hub_hash = hub_hash.lower()
        self.service.remove_rrc_hub(hub_hash)
        if self.current_relay_hub_hash == hub_hash:
            self.content_stack.set_visible_child_name("empty")
            if self.split_view.get_collapsed():
                self.split_view.set_show_sidebar(True)
            self.current_relay_hub_hash = None
            self.current_relay_room = None
            self.action_relay_reconnect.set_enabled(False)
            self.action_relay_copy.set_enabled(False)
            self.action_relay_part.set_enabled(False)
            self.action_relay_add_channel.set_enabled(False)
            self.action_relay_remove_hub.set_enabled(False)
            self.action_relay_add_hub.set_enabled(False)
        self._load_relay_rooms()
        self._show_toast(f"Hub «{hub_name or hub_hash[:8]}» entfernt")

    def _show_add_relay_dialog(self):
        dialog = AddRelayDialog(self, service=self.service, on_hub_added=self._on_relay_hub_added)
        dialog.present()

    def _on_relay_hub_added(self, hub_hex: str, room: Optional[str]):
        self._load_relay_rooms()
        if room:
            self.open_relay_room(hub_hex, room)
        else:
            self.open_relay_hub(hub_hex)

    def _on_rrc_message(self, msg_dict: Dict[str, Any]):
        hub_hash = msg_dict.get("hub_hash", "").lower()
        room = msg_dict.get("room")
        if (self.current_relay_hub_hash and self.current_relay_hub_hash.lower() == hub_hash and
                self.current_relay_room and self.current_relay_room.lower() == (room or "").lower()):
            self.relay_chat_view.add_message(msg_dict)
            self.service.mark_rrc_room_read(hub_hash, room)
        else:
            key = (hub_hash, room)
            if key in self.relay_channel_rows:
                self.relay_channel_rows[key].set_unread(True)

    def _on_rrc_change(self, hub_hex: Optional[str]):
        q = self.search_entry.get_text().strip() if hasattr(self, "search_entry") else None
        active_tab = self.sidebar_stack.get_visible_child_name() if hasattr(self, "sidebar_stack") else "chats"
        if active_tab == "relay":
            self._load_relay_rooms(query=q if q else None)

        if self.current_relay_hub_hash:
            cur_hub_hex = self.current_relay_hub_hash.lower()
            if not hub_hex or hub_hex.lower() == cur_hub_hex:
                hubs = self.service.get_rrc_hubs()
                for h in hubs:
                    if h["hash"].lower() == cur_hub_hex:
                        if self.current_relay_room:
                            self.relay_chat_view.update_status(h["status_text"])
                            members = self.service.get_rrc_members(cur_hub_hex, self.current_relay_room)
                            self.relay_chat_view.update_members(members)
                        else:
                            self.relay_chat_view.load_hub(
                                hub_hash=cur_hub_hex,
                                hub_name=h["name"],
                                status_text=h["status_text"],
                                is_connected=h.get("is_connected", False),
                                motd=h.get("motd"),
                                channels=h.get("rooms", [])
                            )
                        break

    def _action_relay_reconnect(self):
        if self.current_relay_hub_hash:
            self._on_reconnect_relay_hub(self.current_relay_hub_hash)

    def _action_relay_copy_hash(self):
        if self.current_relay_hub_hash:
            self._copy_hub_hash_to_clipboard(self.current_relay_hub_hash)

    def _action_relay_part_room(self):
        if self.current_relay_hub_hash and self.current_relay_room:
            self._on_part_relay_room(self.current_relay_hub_hash, self.current_relay_room)

    def _action_relay_add_channel(self):
        if self.current_relay_hub_hash:
            self._show_join_channel_dialog(self.current_relay_hub_hash)

    def _action_relay_remove_hub(self):
        if self.current_relay_hub_hash:
            self._confirm_remove_relay_hub(self.current_relay_hub_hash)

    # --- Announce Discovery Management ---
    def _load_announces(self, query: Optional[str] = None):
        announces = self.service.get_announces(query=query)
        while child := self.announce_list_box.get_first_child():
            self.announce_list_box.remove(child)
        self.announce_rows.clear()

        if query:
            self.announce_empty_page.set_title("Keine Peers gefunden")
            self.announce_empty_page.set_description(f"Keine Ankündigungen gefunden für «{query}».")
            if hasattr(self, "empty_announce_btn"):
                self.empty_announce_btn.set_visible(False)
        else:
            self.announce_empty_page.set_title("Warte auf Mesh-Peers...")
            self.announce_empty_page.set_description(
                "Sobald Teilnehmer im Reticulum-Netzwerk ein Announce senden, erscheinen sie hier."
            )
            if hasattr(self, "empty_announce_btn"):
                self.empty_announce_btn.set_visible(True)

        for ann in announces:
            row = AnnounceRow(ann, on_start_chat=self._on_announce_start_chat)
            self.announce_rows[ann["destination_hash"]] = row
            self.announce_list_box.append(row)

    def _on_announce_start_chat(self, dest_hash: str, display_name: str):
        self.service.start_conversation(dest_hash)
        self._load_conversations()
        self.open_conversation(dest_hash)

    # --- Sending Messages ---
    def _on_send_message(self, dest_hash: str, content: str, image_path: Optional[str] = None):
        try:
            msg_data = self.service.send_message(dest_hash, content, image_path=image_path)
            self.chat_view.append_message(msg_data)

            # Update conversation list
            self._update_or_add_conv_row(dest_hash)
        except Exception as e:
            self._show_toast(f"Fehler beim Senden: {e}")

    def _update_or_add_conv_row(self, dest_hash: str):
        conv = self.service.get_conversation(dest_hash)
        if not conv:
            return

        if dest_hash in self.conv_rows:
            self.conv_rows[dest_hash].update_data(conv)
        else:
            row = ConversationRow(conv)
            self.conv_rows[dest_hash] = row
            self.conv_list_box.prepend(row)

    # --- Service Callbacks (Runs on GLib Main Loop) ---
    def _on_service_message_received(self, msg_data: Dict[str, Any]):
        sender = msg_data.get("sender_hash", "").lower()

        # Update sidebar
        self._update_or_add_conv_row(sender)

        # If currently viewing this chat, append message
        if self.current_dest_hash and self.current_dest_hash == sender:
            self.chat_view.append_message(msg_data)
            self.service.mark_read(sender)
            if sender in self.conv_rows:
                conv = self.service.get_conversation(sender)
                if conv:
                    self.conv_rows[sender].update_data(conv)

    def _on_service_message_state(self, message_hash: str, state: int):
        self.chat_view.update_message_state(message_hash, state)

    def _on_service_announce_received(self, announce_data: Dict[str, Any]):
        dest_hash = announce_data["destination_hash"].lower()
        active_tab = self.sidebar_stack.get_visible_child_name() if hasattr(self, "sidebar_stack") else "chats"
        q = self.search_entry.get_text().strip()
        if active_tab == "discover" and q:
            self._load_announces(query=q)
        else:
            if dest_hash in self.announce_rows:
                self.announce_rows[dest_hash].update_data(announce_data)
            else:
                row = AnnounceRow(announce_data, on_start_chat=self._on_announce_start_chat)
                self.announce_rows[dest_hash] = row
                self.announce_list_box.prepend(row)

    def _on_service_path_resolved(self, dest_hex: str, hops: int):
        dest_hex = dest_hex.lower()
        conv = self.service.get_conversation(dest_hex)
        if not conv:
            return
        if dest_hex in self.conv_rows:
            self.conv_rows[dest_hex].update_data(conv)
        if self.current_dest_hash == dest_hex:
            self.chat_view.update_header(conv)

    def _on_service_conversations_changed(self):
        q = self.search_entry.get_text().strip() if hasattr(self, "search_entry") else None
        active_tab = self.sidebar_stack.get_visible_child_name() if hasattr(self, "sidebar_stack") else "chats"
        if active_tab == "chats":
            self._load_conversations(query=q if q else None)

    # --- Dialog Openers ---
    def _show_new_chat_dialog(self):
        dialog = NewChatDialog(self, on_chat_created=self._on_new_chat_created)
        dialog.present()

    def _on_new_chat_created(self, dest_hash: str, nickname: Optional[str]):
        dest_hash = dest_hash.lower()
        self.service.start_conversation(dest_hash)
        if nickname:
            self.service.set_custom_name(dest_hash, nickname)
        self._load_conversations()
        self.open_conversation(dest_hash)

    def _show_profile_dialog(self):
        dialog = ProfileDialog(
            self,
            service=self.service,
            on_profile_updated=self._on_profile_updated
        )
        dialog.present()

    def _on_profile_updated(self):
        pass

    def _show_interfaces_dialog(self):
        dialog = InterfacesDialog(self, service=self.service)
        dialog.present()

    # --- Actions ---
    def _action_copy_hash(self):
        if self.current_dest_hash:
            clipboard = Gdk.Display.get_default().get_clipboard()
            clipboard.set(self.current_dest_hash)
            self._show_toast("Zieladresse kopiert!")

    def _action_rename_contact(self):
        if not self.current_dest_hash:
            return
        dest_hash = self.current_dest_hash
        conv = self.service.get_conversation(dest_hash)
        current_name = (conv.get("custom_name") or conv.get("display_name") or "") if conv else ""

        dialog = Adw.AlertDialog(
            heading="Kontakt umbenennen",
            body=f"Geben Sie einen Anzeigenamen für diesen Kontakt ein:\n{dest_hash}"
        )
        dialog.add_response("cancel", "Abbrechen")
        dialog.add_response("save", "Speichern")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        entry = Gtk.Entry()
        entry.set_placeholder_text("Neuer Name (leer = Standard)")
        entry.set_text(current_name)
        entry.set_activates_default(True)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        dialog.set_extra_child(entry)

        def on_response(dlg, response):
            if response == "save":
                new_name = entry.get_text().strip()
                self.service.set_custom_name(dest_hash, new_name if new_name else None)
                self._load_conversations()
                updated_conv = self.service.get_conversation(dest_hash)
                if updated_conv and hasattr(self.chat_view, "update_header"):
                    self.chat_view.update_header(updated_conv)
                if new_name:
                    self._show_toast(f"Kontakt umbenannt in «{new_name}»")
                else:
                    self._show_toast("Kontaktname zurückgesetzt")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _action_request_path(self):
        if not self.current_dest_hash:
            return
        dest_hex = self.current_dest_hash
        short_hash = f"{dest_hex[:8]}...{dest_hex[-4:]}"
        self._show_toast(f"Pfadanfrage für {short_hash} im Mesh gesendet...")

        def on_path_result(target_hex: str, hops: Optional[int], is_new: bool):
            if hops is not None:
                if hops == 0:
                    hops_desc = "Direkt erreichbar (0 Hops)"
                else:
                    hops_desc = f"{hops} Hop{'s' if hops > 1 else ''} entfernt"

                if is_new:
                    self._show_toast(f"Pfad bestätigt: {hops_desc}")
                else:
                    self._show_toast(f"Bekannter Pfad: {hops_desc}")
            else:
                self._show_toast(f"Keine Antwort für {short_hash} (Ziel evtl. offline)")

            conv = self.service.get_conversation(target_hex)
            if conv:
                if target_hex in self.conv_rows:
                    self.conv_rows[target_hex].update_data(conv)
                if self.current_dest_hash == target_hex:
                    self.chat_view.update_header(conv)

        self.service.request_path(dest_hex, callback=on_path_result)

    def _action_sync(self):
        if not self.current_dest_hash:
            return
        dest_hex = self.current_dest_hash
        success, msg = self.service.request_sync(target_node=dest_hex)
        self._show_toast(msg)
        if not success:
            return

        # Poll sync status periodically until finished
        start_time = time.time()
        last_status = [msg]

        def _check_sync_progress():
            if time.time() - start_time > 45:
                return False

            status_raw = self.service.app.get_sync_status() if self.service and self.service.app else "Idle"
            status_text = self.service.get_sync_status_text()

            if status_text != last_status[0]:
                last_status[0] = status_text
                if status_raw not in ("Idle", "Path requested"):
                    self._show_toast(status_text)

            finished_states = (
                "Done, no new messages",
                "Sync failed",
                "No path to node",
                "Link establisment failed",
                "Sync request failed",
                "Node rejected request",
                "Remote got no identity",
            )
            if status_raw in finished_states or (status_raw and status_raw.startswith("Downloaded ")):
                self._load_conversations()
                if self.current_dest_hash:
                    conv = self.service.get_conversation(self.current_dest_hash)
                    messages = self.service.get_messages(self.current_dest_hash)
                    if conv:
                        self.chat_view.load_conversation(conv, messages)
                return False

            return True

        GLib.timeout_add(1500, _check_sync_progress)

    def _action_announce_self(self):
        try:
            self.service.announce()
            self._show_toast("Announce wurde im Reticulum-Mesh gesendet!")
        except Exception as e:
            self._show_toast(f"Fehler beim Announce: {e}")

    def _show_toast(self, text: str):
        toast = Adw.Toast.new(text)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)
